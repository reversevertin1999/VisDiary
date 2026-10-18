"""质检（第四轮）：盲测表情 + 身份特征编号核对。并行逐格检查，不合格自动重画。

输入防线（A2/A3）：调 provider 前核对图片存在；模型回复要写明实际收到几张图、
能否判断，对不上或判不了 → QCInputError，重试 1 次后标记 qc_error（不计入任何统计）。
特征按编号匹配（A4）：模型改写特征名也不影响判定；漏项/重号按 fail 算。
"""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from visdiary import budget
from visdiary.character.store import characters_dir
from visdiary.config import Config
from visdiary.providers.base import ImageProvider, ProviderOutputError, VisionProvider
from visdiary.render import generator
from visdiary.schemas import (EMOTION_GROUPS, EMOTIONS, Character, ExpressionCheck,
                              Panel, QCResult, Storyboard, normalize_emotion)
from visdiary.styles import load_emotions

QC_PROMPT_2IMG = """第一张图是角色 {name} 的标准参考图，第二张是待检查的漫画格。
逐条核对第二张图里的 {name} 是否符合以下特征（按编号回答）：
{checklist}
另外检查：
- has_text：画面里是否出现了文字、对话框或水印
- character_visible：{name} 是否出现在第二张图中
盲评表情（不要假设应该是什么情绪，只描述你看到的）：
- expression.detected_emotion：画面里主角最像的情绪，从这些里选：{emotion_list}
- expression.detected_top2：第二像的情绪
- expression.detected_intensity：1 轻微 / 2 明显 / 3 夸张
- expression.face_size：主角脸在画面中的大小（large/medium/small/not_visible）
输入核对（必须如实回答）：
- images_received：你实际收到了几张图
- judgeable：图是否完整可判断；图缺失/损坏/看不清就填 false 并写 unjudgeable_reason
特征看不清（背影、远景）时该条填 not_visible，不算失败。
只按 JSON schema 输出。"""

QC_PROMPT_1IMG = """这张图是要检查的漫画格。
逐条核对图中的角色是否符合以下特征（按编号回答；特征参考图没有提供，只按文字判断）：
{checklist}
另外检查：
- has_text：画面里是否出现了文字、对话框或水印
- character_visible：角色是否出现在图中
盲评表情（不要假设应该是什么情绪，只描述你看到的）：
- expression.detected_emotion：画面里主角最像的情绪，从这些里选：{emotion_list}
- expression.detected_top2：第二像的情绪
- expression.detected_intensity：1 轻微 / 2 明显 / 3 夸张
- expression.face_size：主角脸在画面中的大小（large/medium/small/not_visible）
输入核对（必须如实回答）：
- images_received：你实际收到了几张图
- judgeable：图是否完整可判断；图缺失/损坏/看不清就填 false 并写 unjudgeable_reason
只按 JSON schema 输出。"""

SCENE_ONLY_PROMPT = """检查这张漫画格：
- has_text：画面里是否出现了文字、对话框或水印
画面里没有需要核对的主角，character_visible 一律填 false，expression 填 null。
- images_received：你实际收到了几张图
- judgeable：图是否完整可判断
只按 JSON schema 输出。"""

FACE_REQUIRED_SHOTS = {"近景", "特写"}  # 只有这两个景别要求脸够大


class QCInputError(Exception):
    """质检输入异常：模型没收到全部图片，或图不可判断。"""


def _numbered_checklist(char: Character) -> tuple[str, dict[int, str]]:
    """特征编号：prompt 里写 1. 2. 3.，返回编号->特征名映射。"""
    lines = [f"{i}. {f.name}：{f.value}" for i, f in enumerate(char.identity, 1)]
    return "\n".join(lines), {i: f.name for i, f in enumerate(char.identity, 1)}


def check_panel(cfg: Config, vision: VisionProvider, char: Character,
                panel: Panel, panel_img: Path, front_ref: Path | None,
                has_protagonist: bool) -> QCResult:
    if has_protagonist:
        checklist, _ids = _numbered_checklist(char)
        emo_list = "、".join(EMOTIONS)
        extra_block = ""
        from visdiary.character.store import load_character as _lc
        others = [c for c in panel.characters if c != char.id]
        if others:  # M19：配角特征编号 101+，QCCheck.character_id 填角色 id
            o = _lc(cfg, others[0])
            olines = [f"{100 + i}. {f.name}：{f.value}" for i, f in enumerate(o.identity, 1)]
            extra_block = (f"\n配角 {o.name}（id={o.id}）的特征（编号 101 起，"
                           f"character_id 填 {o.id!r}）：\n" + "\n".join(olines)
                           + "\n另外：cross_contamination = 两个角色的发色/瞳色是否互串")
        if extra_block:
            checklist += (f"\n主角 {char.name} 的 character_id 一律填 {char.id!r}。"
                          + extra_block)
        if front_ref is not None:
            prompt = QC_PROMPT_2IMG.format(name=char.name, checklist=checklist,
                                           emotion_list=emo_list)
            images = [front_ref, panel_img]
        else:
            prompt = QC_PROMPT_1IMG.format(checklist=checklist, emotion_list=emo_list)
            images = [panel_img]
    else:
        prompt = SCENE_ONLY_PROMPT
        images = [panel_img]

    result = None
    _qc_t0 = time.monotonic()
    for attempt in range(2):  # A3：输入核对不过就重试 1 次
        try:
            result = vision.analyze_json(prompt, images, QCResult)
        except ProviderOutputError:
            budget.record(cfg, vision_calls=1)  # A15：失败的调用也真实发生了
            time.sleep(3)
            continue
        budget.record(cfg, vision_calls=1)
        if result.images_received == len(images) and result.judgeable:
            result._qc_elapsed = time.monotonic() - _qc_t0  # P0-4：真实质检耗时
            return result
        reason = result.unjudgeable_reason or \
            f"声称收到 {result.images_received} 张（实际给了 {len(images)}）"
        if attempt == 0:
            time.sleep(3)
            continue
        raise QCInputError(f"第 {panel.id} 格质检输入异常：{reason}")
    if result is None:
        raise QCInputError(f"第 {panel.id} 格质检两次输出都不合规")
    return result


def identity_passes(result: QCResult, char: Character, has_protagonist: bool,
                    cfg: Config | None = None,
                    panel: Panel | None = None,
                    ) -> tuple[bool, list[str], list[str]]:
    """按特征编号核对（A4 + F4 多角色）。返回 (是否通过, 致命问题, 仅警告的问题)。"""
    if not has_protagonist:
        return not result.has_text, [], []
    problems: list[str] = []
    notes: list[str] = []
    if not result.character_visible:
        return False, ["主角不在画面中"], []

    # M19：串色检查
    if result.cross_contamination:
        problems.append("两个角色的特征互串（cross_contamination）")

    def check_one(ch: Character, cid_prefix: int, who: str):
        crit = {i: f for i, f in enumerate(ch.identity, 1) if f.critical}
        ids = {i for i in crit}
        got: dict[int, list] = {}
        HERO_ALIASES = {"main", "hero", "protagonist", "主角", char.id}
        for c in result.checks:
            raw = c.character_id or ""
            if c.feature_id >= 100:
                # 配角特征编号 101+：编号即归属，cid 摇摆不影响分桶
                belongs_to_side = True
            else:
                belongs_to_side = False
            if cid_prefix == 0:
                ok_own = not belongs_to_side and (
                    (raw or char.id) in HERO_ALIASES)
            else:
                ok_own = belongs_to_side or raw == who
            if not ok_own:
                continue
            fid = c.feature_id - cid_prefix
            if fid in ids:
                got.setdefault(fid, []).append(c)
            elif c.feature_id != 0 and cid_prefix == 0:  # 越界只警告
                notes.append(f"编号 {c.feature_id} 越界（{who} 共 {len(ch.identity)} 条），已忽略")
        for fid, feat in crit.items():
            hits = got.get(fid, [])
            if not hits:
                problems.append(f"{who} 漏了 critical「{feat.name}」，按 fail 算")
            elif len(hits) > 1:
                problems.append(f"{who} 编号 {fid} 重复 {len(hits)} 次，按 fail 算")
            elif any(h.result == "fail" for h in hits):
                problems.append(f"{who} {feat.name} 不符")

    check_one(char, 0, char.name)
    # 配角（编号 101+）。判官 character_id 填的是角色 id，who 也要用 id 匹配
    if cfg is not None and panel is not None:
        from visdiary.character.store import load_character as _lc
        for oid in [c for c in panel.characters if c != char.id]:
            try:
                o = _lc(cfg, oid)
            except FileNotFoundError:
                continue
            check_one(o, 100, oid)
    if result.has_text:
        problems.append("画面出现文字")
    return not problems, problems, notes


def _same_group(a: str, b: str) -> bool:
    return a == b or any(a in g and b in g for g in EMOTION_GROUPS)


def expression_fail_reason(result: QCResult, panel: Panel, has_protagonist: bool,
                           ) -> str | None:
    """表情判定：None=合格；"n/a"=不适用（不计入命中率）；
    否则返回失败类型 category/weak/strong/face。"""
    if not has_protagonist:
        return None
    if not result.judgeable or not result.character_visible:  # A5：先判可判性
        return "n/a"
    if result.expression is None:  # 可见但判官没给表情结论：同样不适用
        return "n/a"
    ex: ExpressionCheck = result.expression
    target = panel.acting
    detected = normalize_emotion(ex.detected_emotion)
    if detected != target.emotion and not _same_group(target.emotion, detected):
        return "category"
    if ex.detected_intensity < target.intensity - 1:
        return "weak"
    if ex.detected_intensity > target.intensity + 1 or \
            (target.intensity == 1 and ex.detected_intensity > 2):
        return "strong"
    if ex.face_size in ("small", "not_visible") and panel.shot in FACE_REQUIRED_SHOTS:
        return "face"
    return None


def _identity_fix_note(result: QCResult, char: Character, problems: list[str],
                       cfg: Config | None = None, panel: Panel | None = None) -> str:
    values = {f.name: f.value for f in char.identity}
    notes = []
    # 配角特征表（101+ 编号用）
    side_values: dict[str, str] = {}
    if cfg is not None and panel is not None:
        from visdiary.character.store import load_character as _lc
        for oid in [c for c in panel.characters if c != char.id]:
            try:
                o = _lc(cfg, oid)
                for f in o.identity:
                    side_values[f.name] = f.value
            except FileNotFoundError:
                continue
    for c in result.checks:  # 修模型明说不符的特征（主角 1..N + 配角 101+）
        if c.result != "fail":
            continue
        if 1 <= c.feature_id <= len(char.identity):
            name = char.identity[c.feature_id - 1].name
            notes.append(f"{name} must be {values.get(name, c.note)}")
        elif c.feature_id >= 101 and c.name in side_values:
            # P1-4-5：配角失败也要生成修正提示
            notes.append(f"{c.name} must be {side_values[c.name]}")
    notes += [f"make sure {p}" for p in problems if "漏了" in p or "重复" in p]
    if result.has_text:
        notes.append("no text, no speech bubbles, no watermark in the image")
    if not result.character_visible:
        notes.append(f"{char.name} must appear in the frame")
    return "; ".join(n for n in notes if n)


def _expression_fix_note(cfg: Config, panel: Panel, reason: str) -> str:
    """按失败类型写修正提示：演过头要收敛，不够要加戏，脸小要拉近。"""
    emotions, _ = load_emotions(cfg)
    a = panel.acting
    face = emotions[a.emotion].levels[a.intensity].face
    en = emotions[a.emotion].en
    if reason == "weak":
        return f"make the expression much more exaggerated: {face}"
    if reason == "strong":
        return f"make the expression more restrained and subtle: {face}"
    if reason == "face":
        return "make the character's face larger in frame"
    return f"the expression should clearly read as {en}: {face}"


def _front_ref(cfg: Config, char: Character, style_id: str) -> Path | None:
    sheet = char.sheets.get(style_id)
    if sheet and "front_full" in sheet.views:
        p = characters_dir(cfg) / char.id / sheet.views["front_full"]
        return p if p.is_file() else None
    return None


def _qc_one(cfg: Config, vision: VisionProvider, image: ImageProvider,
            char: Character, panel: Panel, style_id: str, outdir: Path,
            metas: dict[int, dict], front_ref: Path | None,
            extras: dict[str, str], deadline: float | None = None) -> dict:
    # C4 live 档：纯场景格只查文字，不做完整质检（省时）
    if getattr(cfg.qc, "scene_only_fast", False) and char.id not in panel.characters:
        meta = metas[panel.id]
        meta["qc"] = {"note": "live: 纯场景格仅记录"}
        generator.save_meta(outdir, meta)
        return {"panel": panel.id, "passed": not meta.get("flagged", False),
                "retries": 0, "flagged": meta.get("flagged", False),
                "result": None, "expression_pass": None, "expression_fail": None,
                "reused": "scene-only"}
    meta = metas[panel.id]
    img_path = outdir / "panels" / f"{panel.id:02d}.png"
    if meta.get("error"):  # 生图失败的占位格
        return {"panel": panel.id, "passed": False, "retries": 0, "flagged": True,
                "result": None, "expression_pass": None, "error": meta["error"],
                "_meta": meta}
    if meta.get("_reused"):  # 锁定格/已接受格：沿用旧质检结果
        return {"panel": panel.id, "passed": True, "retries": 0, "flagged": False,
                "result": meta.get("qc"), "expression_pass": None,
                "expression_fail": None, "reused": meta["_reused"], "_meta": meta}

    has_protagonist = char.id in panel.characters
    retries = 0
    result = None
    id_problems: list[str] = []
    fail_reason = None
    try:
        qc_total = 0.0
        while True:
            result = check_panel(cfg, vision, char, panel, img_path, front_ref,
                                 has_protagonist)
            qc_total += getattr(result, "_qc_elapsed", 0.0)
            id_ok, id_problems, id_notes = identity_passes(
                result, char, has_protagonist, cfg=cfg, panel=panel)
            fail_reason = expression_fail_reason(result, panel, has_protagonist)
            expression_blocks = fail_reason is not None and cfg.qc.expression_strict
            if id_ok and not expression_blocks:
                break
            if retries >= cfg.qc.max_retries:
                meta["flagged"] = True
                break
            if deadline is not None and time.monotonic() > deadline:  # P0-2 超时
                meta["flagged"] = True
                meta["timeout"] = True
                break
            retries += 1
            if not id_ok:
                note = _identity_fix_note(result, char, id_problems,
                                          cfg=cfg, panel=panel)  # 身份优先
            else:
                note = _expression_fix_note(cfg, panel, fail_reason)
            try:  # A11：重画不用 crop_hint（旧 bbox 对不上新图）
                meta = generator.generate_panel(cfg, image, char, panel, meta["aspect"],
                                                style_id, extras, outdir, fix_note=note,
                                                attempt=retries)
            except Exception as e:  # 重画失败保留上一张图
                meta["flagged"] = True
                meta["redraw_error"] = str(e)[:200]
                generator.save_meta(outdir, meta)
                break
            metas[panel.id] = meta
            img_path = outdir / "panels" / f"{panel.id:02d}.png"
    except QCInputError as e:  # A3：输入异常 → qc_error，不计入任何统计
        meta["qc_error"] = str(e)
        generator.save_meta(outdir, meta)
        return {"panel": panel.id, "passed": False, "retries": 0, "flagged": True,
                "qc_error": str(e), "result": None, "expression_pass": None,
                "expression_fail": None, "_meta": meta}
    except Exception as e:  # P1-2：网络等未知异常也按格隔离，不让整页崩
        meta["qc_error"] = f"{type(e).__name__}: {e}"[:200]
        generator.save_meta(outdir, meta)
        return {"panel": panel.id, "passed": False, "retries": 0, "flagged": True,
                "qc_error": meta["qc_error"], "result": None, "expression_pass": None,
                "expression_fail": None, "_meta": meta}

    meta["qc"] = json.loads(result.model_dump_json())
    meta["qc_retries"] = retries
    meta["qc_s"] = round(qc_total, 1)   # P0-4：真实质检耗时进 meta
    meta["accepted_key"] = meta.get("cache_key") if not meta["flagged"] else None
    generator.save_meta(outdir, meta)
    return {
        "panel": panel.id, "passed": not meta["flagged"], "retries": retries,
        "flagged": meta["flagged"],
        "result": json.loads(result.model_dump_json()),
        "identity_problems": id_problems + id_notes,
        "expression_pass": (fail_reason is None) if fail_reason != "n/a" else None,
        "expression_fail": fail_reason,
        "_meta": meta,   # P0-3：把重画后的最终 meta 带回去
    }


def qc_all(cfg: Config, vision: VisionProvider, image: ImageProvider,
           char: Character, sb: Storyboard, style_id: str, outdir: Path,
           metas: dict[int, dict], progress=None) -> dict:
    """并行逐格质检，写出 qc_report.json（盲测指标 + qc_error_count）。"""
    front_ref = _front_ref(cfg, char, style_id)
    with ThreadPoolExecutor(max_workers=cfg.generation.concurrency) as pool:
        futures = {
            pool.submit(_qc_one, cfg, vision, image, char, p, style_id,
                        outdir, metas, front_ref, sb.extras_map()): p.id
            for p in sb.panels
        }
        entries = []
        for f in futures:
            entries.append(f.result())
            if progress:
                progress(f"质检 {len(entries)}/{len(futures)}")
    entries.sort(key=lambda e: e["panel"])

    valid = [e for e in entries if not e.get("qc_error")]
    qc_error_count = len(entries) - len(valid)
    total = len(valid) or 1
    first_pass = sum(1 for e in valid if e["retries"] == 0 and e["passed"])
    final_pass = sum(1 for e in valid if e["passed"])
    expr = [e for e in valid if e["expression_pass"] is not None]
    expr_hits = sum(1 for e in expr if e["expression_pass"])
    overacted = sum(1 for e in expr if e["expression_fail"] == "strong")
    # §3.2：强度按检测结果算 MAE / 偏差（n/a 不进分母）
    panels_by_id = {p.id: p for p in sb.panels}
    int_rows = [e for e in expr
                if e.get("result") and e["result"].get("expression")]
    diffs = [e["result"]["expression"]["detected_intensity"]
             - panels_by_id[e["panel"]].acting.intensity for e in int_rows]
    report = {
        "panels": entries,
        "qc_error_count": qc_error_count,
        "expression_na_count": sum(1 for e in valid
                                   if e.get("expression_fail") == "n/a"),
        "first_pass_rate": round(first_pass / total, 3),
        "final_pass_rate": round(final_pass / total, 3),
        "expression_hit_rate": round(expr_hits / len(expr), 3) if expr else None,
        "overacted_rate": round(overacted / len(expr), 3) if expr else None,
        "flagged": [e["panel"] for e in entries if e["flagged"]],
        "intensity_mae": round(sum(abs(x) for x in diffs) / len(diffs), 3) if diffs else None,
        "intensity_bias": round(sum(diffs) / len(diffs), 3) if diffs else None,
    }
    (outdir / "qc_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def generate_and_qc_all(cfg, image, vision, char, sb, layout, style_id, outdir,
                        reuse_metas=None, progress=None) -> tuple[dict, dict]:
    """C2 流水线化：一格生完立即进自己的质检+重画，共用一个线程池。

    整页耗时 ≈ 最慢一格的（生图+质检+重画），而不是"全部生图 + 全部质检"之和。
    C3：单格总预算 generation.panel_timeout_s，超时保留当前最好的图并 flagged。
    返回 (metas, qc_report)。
    """
    import concurrent.futures as cf
    import time as _t

    from visdiary.render import generator as G

    aspect = {b.id: b.aspect for b in layout.panels}
    extras = sb.extras_map()
    front_ref = _front_ref(cfg, char, style_id)
    from visdiary import budget as _B0
    _v_before = _B0.usage(cfg, cfg.env)["vision_calls"]  # P0-4
    results: dict[int, dict] = {}
    entries: list[dict] = []
    lock = threading.Lock()

    def pipeline_one(panel):
        t0 = _t.monotonic()
        deadline = t0 + cfg.generation.panel_timeout_s
        old = (reuse_metas or {}).get(panel.id)
        png = outdir / "panels" / f"{panel.id:02d}.png"

        # 1) 生图（复用逻辑同 generate_all：locked/accepted 直接用）
        try:
            if old and old.get("locked") and png.is_file():
                old["_reused"] = "locked"
                meta = old
            elif old and old.get("accepted_key"):
                from visdiary.render import cache as _cache
                data = _cache.get(cfg, old["accepted_key"])
                if data is not None:
                    png.write_bytes(data)
                    old["cache_hit"] = True
                    old["_reused"] = "accepted"
                    meta = old
                else:
                    meta = G.generate_panel(cfg, image, char, panel, aspect[panel.id],
                                            style_id, extras, outdir)
            else:
                meta = G.generate_panel(cfg, image, char, panel, aspect[panel.id],
                                        style_id, extras, outdir)
            if progress:
                progress(f"生图 第 {panel.id} 格完成")
        except Exception as e:
            meta = G._failed_placeholder(cfg, image, sb, panel.id, aspect[panel.id],
                                         outdir, str(e))

        # 2) 本格质检 + 重画（带超时预算 P0-2）
        try:
            entry = _qc_one(cfg, vision, image, char, panel, style_id, outdir,
                            {panel.id: meta}, front_ref, extras, deadline=deadline)
        except Exception as e:  # 超时或意外：保留当前图，标记
            meta["flagged"] = True
            meta["qc_error"] = f"pipeline: {e}"[:200]
            G.save_meta(outdir, meta)
            entry = {"panel": panel.id, "passed": False, "retries": 0,
                     "flagged": True, "result": None, "expression_pass": None,
                     "expression_fail": None, "qc_error": meta["qc_error"]}
        entry["panel_s"] = round(_t.monotonic() - t0, 1)
        meta = entry.pop("_meta", meta)   # P0-3：用重画后的最终 meta
        with lock:
            results[panel.id] = meta
            entries.append(entry)
            if progress:
                progress(f"质检 第 {panel.id} 格完成"
                         f"（{'✓' if entry['passed'] else '✗'}）")

    with cf.ThreadPoolExecutor(max_workers=cfg.generation.concurrency) as pool:
        futs = [pool.submit(pipeline_one, p) for p in sb.panels]
        for f in futs:
            f.result()

    entries.sort(key=lambda e: e["panel"])
    from visdiary import budget as _B
    _v_after = _B.usage(cfg, cfg.env)["vision_calls"]
    valid = [e for e in entries if not e.get("qc_error")]
    total = len(valid) or 1
    expr = [e for e in valid if e["expression_pass"] is not None]
    report = {
        "panels": entries,
        "qc_error_count": len(entries) - len(valid),
        "first_pass_rate": round(sum(1 for e in valid if e["retries"] == 0 and e["passed"]) / total, 3),
        "final_pass_rate": round(sum(1 for e in valid if e["passed"]) / total, 3),
        "expression_hit_rate": (round(sum(1 for e in expr if e["expression_pass"]) / len(expr), 3)
                                if expr else None),
        "flagged": [e["panel"] for e in entries if e["flagged"]],
    }
    report["vision_calls"] = _v_after - _v_before  # P0-4：budget 实测差值
    (outdir / "qc_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return results, report
