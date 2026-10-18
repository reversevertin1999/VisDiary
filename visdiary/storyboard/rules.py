"""分镜的代码层规则校验：不完全相信 LLM 输出，逐条检查并自动修复。

- 能安全自动修的：修 + warning（格数、镜头、文本长度等）
- 情绪节奏类（改错比不改更糟）：抛 EmotionRuleError 让 LLM 重试一次，
  仍不达标则降级为 warning 放行（builder 用 soft=True 再跑一遍）
- 人设（persona）：强度按 expressiveness 缩放并夹到上限；banned 中的
  情绪/漫符/chibi 被拦下；阈值随人设调整
- warning 里的格号以重新编号后的 id 为准
"""

import re

from visdiary.config import StoryboardConfig
from visdiary.schemas import (EMOTION_GROUPS, EMOTIONS, MANPUS, SHOTS, Persona,
                              Storyboard, normalize_emotion)

TEXT_LIMIT = 28  # 旁白 / 对白的字数上限（含截断时补的省略号）
MAX_DIALOGUE = 2
CALM_MAX_RATIO = 0.3
STRONG_SHOTS = {"近景", "特写", "仰视"}  # 强情绪/高光格允许的镜头

_CJK = re.compile(r"[一-鿿]")

# 景别距离：数值越大脸越大。俯视/仰视/背影不参与距离，按中景档处理
SHOT_DISTANCE = {"远景": 0, "全景": 1, "中景": 2, "近景": 3, "特写": 4,
                 "俯视": 2, "仰视": 2, "背影": 2}

# visual_prompt_en 英文情绪词 -> 兼容的情绪组；与 acting.emotion 不兼容即冲突
_EMOTION_KEYWORDS = [
    ("smiling|happy", {"开心", "大笑", "得意", "期待", "感动"}),
    ("laughing", {"大笑", "开心"}),
    ("crying|tears|weeping", {"哭", "大哭", "感动", "难过", "委屈"}),
    ("sad|sadness", {"难过", "委屈"}),
    ("angry|furious|mad", {"生气", "暴怒"}),
    ("surprised", {"惊讶", "震惊"}),
    ("shocked", {"震惊", "崩溃"}),
    ("tired|exhausted", {"疲惫"}),
    ("calm", {"平静"}),
    ("blushing|shy", {"害羞"}),
    ("thinking", {"思考"}),
    ("smug", {"得意"}),
    ("nervous|anxious", {"紧张"}),
    ("determined", {"坚定"}),
    ("speechless|deadpan", {"无语"}),
]
_NEGATION = re.compile(r"(?:not|no|without|never|un)\s*$", re.I)


class StoryboardError(Exception):
    pass


class EmotionRuleError(StoryboardError):
    """情绪节奏不达标：重试一次，再不行只 warning。"""


def _replace_shot(current: str, used: set[str], prefer: list[str] | None = None) -> str:
    """相邻镜头去重：优先用词表里该情绪的推荐镜头，否则选景别距离最近的。"""
    for s in (prefer or []):
        if s not in used:
            return s
    candidates = [s for s in SHOTS if s not in used]
    if not candidates:
        candidates = [s for s in SHOTS if s != current]
    d = SHOT_DISTANCE[current]
    return min(candidates, key=lambda s: (abs(SHOT_DISTANCE[s] - d), -SHOT_DISTANCE[s]))


def _apply_persona(panels, persona: Persona, warnings: list[str]) -> None:
    """人设生效：禁用的归位，强度按 expressiveness 缩放并夹到上限。"""
    changed = []
    e = persona.expressiveness
    for p in panels:
        a = p.acting
        # banned 情绪归到同组里第一个允许的；都不行就平静（平静被禁则开心）
        if a.emotion in persona.banned:
            new_emo = next((m for g in EMOTION_GROUPS if a.emotion in g
                            for m in g if m not in persona.banned), None)
            if new_emo is None:
                new_emo = "平静" if "平静" not in persona.banned else "开心"
            changed.append(f"第 {p.id} 格 {a.emotion} 被禁用，改为{new_emo}")
            a.emotion = new_emo
        # 强度缩放 + 上限
        old = a.intensity
        a.intensity = max(1, min(3, round(old * e)))
        cap = persona.max_intensity.get(a.emotion)
        if cap is not None and a.intensity > cap:
            a.intensity = cap
        if a.intensity != old:
            changed.append(f"第 {p.id} 格强度 {old}→{a.intensity}")
        # banned 漫符 / chibi
        if a.manpu:
            kept = [m for m in a.manpu if m not in persona.banned]
            if len(kept) != len(a.manpu):
                changed.append(f"第 {p.id} 格漫符 {[m for m in a.manpu if m not in kept]} 被禁用，已移除")
                a.manpu = kept
        if a.chibi and "chibi" in persona.banned:
            a.chibi = False
            changed.append(f"第 {p.id} 格 chibi 被禁用，已关闭")
    if changed:
        warnings.append("人设生效：" + "；".join(changed))


def _thresholds(persona: Persona) -> tuple[float, int]:
    """按 expressiveness 调整阈值：内向的角色允许更多平静、高光不必满级。"""
    e = persona.expressiveness
    calm_max = min(1.0, CALM_MAX_RATIO / e)
    hmi = max(1, min(3, round(3 * e)))
    return calm_max, hmi


def _check_shots(panels, warnings, hmi: int, emotions: dict | None = None):
    for p in panels:
        a = p.acting
        if not p.characters:
            continue  # 纯场景格不锁镜头
        # 强情绪锁近景：intensity=3 或高光格，脸必须够大
        if (a.intensity == 3 or p.weight == "highlight") and p.shot not in STRONG_SHOTS:
            warnings.append(f"第 {p.id} 格是强情绪/高光格，镜头{p.shot}看不到表情，已改为近景")
            p.shot = "近景"
        # 背影不配强表情
        if p.shot == "背影" and a.intensity > 1:
            warnings.append(f"第 {p.id} 格背影看不到 {a.intensity} 级表情，已改为中景")
            p.shot = "中景"
    # 相邻镜头不重复（在强情绪锁定之后做，替换时避开前后格，优先词表推荐镜头）
    for i in range(1, len(panels)):
        if panels[i].shot == panels[i - 1].shot:
            used = {panels[i - 1].shot}
            if i + 1 < len(panels):
                used.add(panels[i + 1].shot)
            prefer = emotions[panels[i].acting.emotion].shots if emotions else None
            new_shot = _replace_shot(panels[i].shot, used, prefer)
            # 强情绪格不能被改成远景/背影
            a = panels[i].acting
            if (a.intensity == 3 or panels[i].weight == "highlight") and panels[i].characters \
                    and new_shot not in STRONG_SHOTS:
                new_shot = next((s for s in ("近景", "特写", "仰视") if s not in used), "近景")
            warnings.append(f"第 {i}/{i + 1} 格镜头重复（{panels[i].shot}），"
                            f"第 {i + 1} 格改为{new_shot}")
            panels[i].shot = new_shot


def _check_rhythm(panels, calm_max: float, hmi: int):
    """情绪节奏检查，返回问题列表（由调用方决定重试还是 warning）。"""
    problems = []
    emotions = [p.acting.emotion for p in panels]
    if len(panels) >= 6 and len(set(emotions)) < 3:
        problems.append(f"{len(panels)} 格只有 {len(set(emotions))} 种情绪，至少 3 种")
    calm = sum(1 for e in emotions if e == "平静")
    if panels and calm / len(panels) > calm_max:
        problems.append(f"平静占比 {calm}/{len(panels)} 超过 {calm_max:.0%}")
    run = 0
    for e in emotions:
        run = run + 1 if e == "平静" else 0
        if run > 2:
            problems.append("平静连续超过 2 格")
            break
    for p in panels:
        if p.weight == "highlight" and p.acting.intensity < hmi:
            problems.append(f"高光格（第 {p.id} 格）intensity 至少 {hmi}，当前是 {p.acting.intensity}")
    return problems


def _warn_emotion_conflicts(p, warnings):
    """visual_prompt_en 与 acting.emotion 矛盾时只 warning 不改文本；
    词边界匹配，否定句（not/no/without）跳过。"""
    for pattern, group in _EMOTION_KEYWORDS:
        for m in re.finditer(rf"\b(?:{pattern})\b", p.visual_prompt_en, re.I):
            if p.acting.emotion in group:
                break
            prefix = p.visual_prompt_en[max(0, m.start() - 30):m.start()]
            prev_words = prefix.split()[-3:]
            if prev_words and _NEGATION.search(" ".join(prev_words) + " "):
                continue  # "not smiling" 这类否定不算冲突
            warnings.append(f"第 {p.id} 格 visual_prompt_en 的 {m.group(0)!r} 与 "
                            f"emotion={p.acting.emotion} 可能矛盾（以 acting 为准，未改文本）")
            break


def _apply_directives(panels, directives: dict[str, dict], persona: Persona,
                      warnings: list[str]) -> None:
    """<!-- vd: --> 导演提示强制执行：情绪/强度直接改，note 写进 face_en（最高优先级）。"""
    for p in panels:
        d = directives.get(p.source)
        if not d:
            continue
        a = p.acting
        if d.get("emotion") and a.emotion != d["emotion"]:
            warnings.append(f"第 {p.id} 格按导演提示改为 {d['emotion']}（原 {a.emotion}）")
            a.emotion = d["emotion"]
        if d.get("intensity") and a.intensity != d["intensity"]:
            warnings.append(f"第 {p.id} 格按导演提示强度改为 {d['intensity']}")
            a.intensity = d["intensity"]
        for ban in d.get("ban", []):
            if a.emotion == ban:
                new_emo = next((m for g in EMOTION_GROUPS if a.emotion in g
                                for m in g if m not in d["ban"]
                                and m not in persona.banned), "平静")
                warnings.append(f"第 {p.id} 格 {ban} 被导演提示禁用，改为{new_emo}")
                a.emotion = new_emo
        if d.get("note"):
            a.face_en = f"{a.face_en}; {d['note']}" if a.face_en else d["note"]


def validate(sb: Storyboard, cfg: StoryboardConfig, known_ids: list[str],
             sources: set[str], persona: Persona | None = None,
             directives: dict[str, dict] | None = None,
             emotions: dict | None = None,
             soft: bool = False) -> tuple[Storyboard, list[str]]:
    warnings: list[str] = []
    persona = persona or Persona()
    calm_max, hmi = _thresholds(persona)
    panels = list(sb.panels)

    # 格数：超出上限时保留高光格，再按顺序截断；低于下限报错重试
    if len(panels) > cfg.max_panels:
        hl = next((p for p in panels if p.weight == "highlight"), None)
        kept, room = [], cfg.max_panels - (1 if hl else 0)
        for p in panels:
            if p is hl:
                continue
            if room > 0:
                kept.append(p)
                room -= 1
        if hl:
            kept.insert(min(panels.index(hl), len(kept)), hl)
        warnings.append(f"格数 {len(panels)} 超出上限 {cfg.max_panels}，已截断并保留高光格")
        panels = kept
    if len(panels) < cfg.min_panels:
        raise StoryboardError(f"格数 {len(panels)} 低于下限 {cfg.min_panels}")

    # 先重新编号，后面的 warning 才能对上最终分镜的格号
    for i, p in enumerate(panels, 1):
        p.id = i

    _apply_persona(panels, persona, warnings)
    _apply_directives(panels, directives or {}, persona, warnings)

    # 恰好 1 个高光格；没有就选情绪强度最高的（并列取对白多的）
    his = [p for p in panels if p.weight == "highlight"]
    if not his:
        target = max(panels, key=lambda p: (p.acting.intensity, len(p.dialogue)))
        target.weight = "highlight"
        target.acting.intensity = max(target.acting.intensity, hmi)  # 满足高光强度下限
        warnings.append(f"没有高光格，已把情绪最强的第 {target.id} 格设为高光")
    elif len(his) > 1:
        for p in his[1:]:
            p.weight = "normal"
        warnings.append(f"有 {len(his)} 个高光格，只保留第一个")

    _check_shots(panels, warnings, hmi, emotions)

    # 情绪节奏：默认抛错让 LLM 重试；soft=True 时只记 warning
    problems = _check_rhythm(panels, calm_max, hmi)
    if problems:
        if soft:
            warnings += [f"情绪节奏未达标：{p}" for p in problems]
        else:
            raise EmotionRuleError("；".join(problems))

    for p in panels:
        # 3 级夸张格没给漫符时用词表推荐值兜底
        if emotions and p.acting.intensity == 3 and not p.acting.manpu:
            rec = emotions[p.acting.emotion].manpu
            if rec:
                p.acting.manpu = rec[:1]
                warnings.append(f"第 {p.id} 格强度 3 缺漫符，已补 {rec[0]}")
        # 角色 id 必须已建档
        unknown = [c for c in p.characters if c not in known_ids]
        if unknown:
            p.characters = [c for c in p.characters if c in known_ids]
            warnings.append(f"第 {p.id} 格包含未知角色 {unknown}，已移除")
        # M19：一格最多 2 个已建档角色，多出的移除
        if len(p.characters) > 2:
            drop = p.characters[2:]
            p.characters = p.characters[:2]
            warnings.append(f"第 {p.id} 格超过 2 个已建档角色，{drop} 移除")
        # 双人格默认中景/全景，不安排特写
        if len(p.characters) == 2 and p.shot in ("特写", "近景"):
            p.shot = "中景"
            warnings.append(f"第 {p.id} 格双人特写改为中景（双人默认中景/全景）")
        # source 必须是日记里出现过的时间或 all
        if p.source not in sources:
            fallback = "all" if "all" in sources else sorted(sources)[0]
            warnings.append(f"第 {p.id} 格 source {p.source!r} 在日记里不存在，已改为 {fallback!r}")
            p.source = fallback
        # 画面描述不应出现中文
        if _CJK.search(p.visual_prompt_en):
            warnings.append(f"第 {p.id} 格 visual_prompt_en 含有中文，生图模型可能理解不了")
        _warn_emotion_conflicts(p, warnings)
        # 对白最多 2 条
        if len(p.dialogue) > MAX_DIALOGUE:
            p.dialogue = p.dialogue[:MAX_DIALOGUE]
            warnings.append(f"第 {p.id} 格对白超过 {MAX_DIALOGUE} 条，已截断")
        # 旁白、对白超长截断，补省略号
        if p.narration and len(p.narration) > TEXT_LIMIT:
            p.narration = p.narration[:TEXT_LIMIT - 1] + "…"
            warnings.append(f"第 {p.id} 格旁白超过 {TEXT_LIMIT} 字，已截断")
        for d in p.dialogue:
            if len(d.text) > TEXT_LIMIT:
                d.text = d.text[:TEXT_LIMIT - 1] + "…"
                warnings.append(f"第 {p.id} 格对白超过 {TEXT_LIMIT} 字，已截断")

    sb.panels = panels
    return sb, warnings
