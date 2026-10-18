"""M16 偏好学习：用户每次 edit / pick 都记进 feedback.jsonl，越用越像她。

- few-shot：拆分镜时把最相近的纠正记录贴进 prompt
- 强度校准：按情绪统计 user−system 平均差，给出 max_intensity 建议（用户确认才生效）
"""

import json
from pathlib import Path

from visdiary.character.store import characters_dir, load_character, save_character
from visdiary.config import Config


def _file(cfg: Config, character_id: str) -> Path:
    return characters_dir(cfg) / character_id / "feedback.jsonl"


def log_feedback(cfg: Config, character_id: str, entry: dict) -> None:
    f = _file(cfg, character_id)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_feedback(cfg: Config, character_id: str) -> list[dict]:
    f = _file(cfg, character_id)
    if not f.is_file():
        return []
    return [json.loads(line) for line in f.read_text(encoding="utf-8").splitlines()
            if line.strip()]


def find_similar(cfg: Config, character_id: str, text: str = "", k: int = 5) -> list[str]:
    """检索最相近的纠正记录，格式化成 few-shot 行（MockLLM 也认这个格式）。"""
    rows = load_feedback(cfg, character_id)
    if not rows:
        return []

    def score(r):
        s = 0
        emo = (r.get("user") or {}).get("emotion")
        if emo and emo in text:
            s += 2
        s += sum(1 for w in ("火锅", "训练", "考试", "加班", "跑", "雨", "睡", "吃")
                 if w in text and w in (r.get("text") or ""))
        return s

    rows = sorted(rows, key=score, reverse=True)[:k]
    lines = []
    for r in rows:
        sys_a, user_a = r.get("system") or {}, r.get("user") or {}
        if not sys_a.get("emotion") or not user_a.get("emotion"):
            continue
        line = (f"情绪「{sys_a['emotion']}」：系统给过 {sys_a.get('intensity', '?')} 级，"
                f"她纠正为 {user_a.get('emotion')} {user_a.get('intensity', '?')} 级")
        if user_a.get("note"):
            line += f"（{user_a['note']}）"
        lines.append(line)
    return lines


def calibration_suggestions(cfg: Config, character_id: str) -> dict[str, int]:
    """某情绪被稳定下调 ≥0.5 级（至少 2 条记录）→ 建议 max_intensity 取用户均值。"""
    rows = load_feedback(cfg, character_id)
    diffs: dict[str, list[int]] = {}
    user_lvls: dict[str, list[int]] = {}
    for r in rows:
        sys_a, user_a = r.get("system") or {}, r.get("user") or {}
        if sys_a.get("emotion") and user_a.get("emotion") == sys_a["emotion"] \
                and user_a.get("intensity") and sys_a.get("intensity"):
            diffs.setdefault(sys_a["emotion"], []).append(
                sys_a["intensity"] - user_a["intensity"])
            user_lvls.setdefault(sys_a["emotion"], []).append(user_a["intensity"])
    out = {}
    for emo, d in diffs.items():
        if len(d) >= 2 and sum(d) / len(d) >= 0.5:
            out[emo] = max(1, min(3, round(sum(user_lvls[emo]) / len(user_lvls[emo]))))
    return out


def apply_suggestions(cfg: Config, character_id: str) -> dict[str, int]:
    """把建议写进 persona.max_intensity。调用前必须给用户看过并确认。"""
    sugg = calibration_suggestions(cfg, character_id)
    char = load_character(cfg, character_id)
    char.persona.max_intensity.update(sugg)
    save_character(cfg, char)
    return sugg
