"""参考图选择（第二轮）：身份图和表情图分开，最多 3 张。

- 身份图：近景/特写用 bust_neutral，其余用 front_full；intensity=3 不用中性脸
- 表情图：emotions.yaml 的 ref_view；背影不给表情图
- 返回 (view_id, path) 列表，view_id 供 prompt 写参考图说明
"""

from pathlib import Path

from visdiary.character.store import characters_dir
from visdiary.config import Config
from visdiary.render.prompt_builder import _expression_view
from visdiary.schemas import Character, Panel
from visdiary.styles import load_emotions

MAX_REFS = 3
CLOSE_SHOTS = {"近景", "特写"}


def select_refs(cfg: Config, char: Character, panel: Panel, style_id: str,
                ) -> list[tuple[str, Path]]:
    """返回 [(角色说明, 路径)]：provider 只做编号，语义在说明里。"""
    # 这一格没有主角（纯场景）时不带参考图
    if char.id not in panel.characters:
        return []
    sheet = char.sheets.get(style_id)
    if not sheet:
        return []

    base = characters_dir(cfg) / char.id

    def path_of(view: str | None) -> Path | None:
        if not view:
            return None
        rel = sheet.views.get(view)
        if not rel:
            return None
        p = base / rel
        return p if p.is_file() else None

    if panel.shot == "背影":
        views = [("back_full", "背面视图"), ("front_full", "身份图（正面全身）")]
        return [(f"{label}（{v}）", p) for v, label in views
                if (p := path_of(v))][:MAX_REFS]

    a = panel.acting
    # intensity=3 只给身份全身图 + 表情图，不加中性脸，避免中和夸张表情
    if a.intensity == 3 or panel.shot not in CLOSE_SHOTS:
        identity = "front_full"
    else:
        identity = "bust_neutral"
    if path_of(identity) is None and identity != "front_full":
        identity = "front_full"
    views = [(identity, "身份图")]
    emotions, _ = load_emotions(cfg)
    expr = _expression_view(char, panel, emotions)  # 人设 override 优先
    if expr and path_of(expr):
        views.append((expr, "表情图"))

    picked: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for v, label in views:
        p = path_of(v)
        if p and p not in seen:
            picked.append((f"{label}（{v}）", p))
            seen.add(p)
    picked = picked[:MAX_REFS]

    # M19：配角身份图（主角用掉 2-3 张预算，配角最多补 1 张，总 ≤ 4）
    others = [c for c in panel.characters if c != char.id][:1]
    if others:
        from visdiary.character.store import characters_dir as _cd
        from visdiary.schemas import Character as _C
        for oid in others:
            other_base = _cd(cfg) / oid
            op = other_base / f"sheets/{style_id}/front_full.png"
            if op.is_file() and op not in seen and len(picked) < 4:
                picked.append((f"配角{oid}身份图（front_full）", op))
    return picked
