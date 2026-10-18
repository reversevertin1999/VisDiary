"""角色建档与定妆：分析角色图 → 生成设定卡 → review.html → approve。

第三轮：设定卡 = 3 张全身视图 + 16 张表情胸像（2×3/2×3/2×2 三张表情表，
投影法找间隙切图，多模态盲校验每格标签）。任何 regen 都会重置 approved，
重新复核后才可用于 render。
"""

import io
import time
from pathlib import Path

from pydantic import BaseModel

from visdiary import budget
from visdiary.character import store
from visdiary.character.prompts import (ANALYSIS_PROMPT, BUST_DESC, BUST_SHEETS,
                                        BUST_VIEWS, FULL_VIEWS, VIEWS,
                                        CharacterAnalysis)
from visdiary.config import Config
from visdiary.providers.base import ImageProvider, VisionProvider
from visdiary.schemas import Character, StyleSheet
from visdiary.styles import load_emotions, load_presets

LOW_CONFIDENCE = 0.6
EXPRESSION_SHEET = "expression_sheet"  # regen 的特殊 view id：只重画表情表


class CellEmotion(BaseModel):
    """表情表切格的盲校验输出。"""
    detected_emotion: str
    detected_top2: list[str] = []


def resolve_style(char: Character, style: str | None = None) -> str:
    """没显式给 style 时：识别画风有卡就用识别画风，否则唯一的画风也接受。"""
    if style:
        return style
    if char.style_detected in char.sheets:
        return char.style_detected
    if len(char.sheets) == 1:
        return next(iter(char.sheets))
    if char.sheets:
        raise ValueError(f"请用 --style 指定画风，已有：{list(char.sheets)}")
    return char.style_detected


def analyze_character(cfg: Config, vision: VisionProvider, image_path: Path,
                      ) -> CharacterAnalysis:
    presets = load_presets(cfg)
    prompt = ANALYSIS_PROMPT.format(style_ids="、".join(presets))
    return vision.analyze_json(prompt, [Path(image_path)], CharacterAnalysis)


# 第三轮 P0-1：设定卡只锁身份，不抄表情和姿势
_IDENTITY_ONLY = ("The reference image shows the character's identity only: keep hair "
                  "style and color, eye color, face shape and accessories consistent. "
                  "Do NOT copy its expression or pose.")


def sheet_prompt(cfg: Config, char: Character, style_id: str, view_id: str) -> str:
    preset = load_presets(cfg)[style_id]
    desc, _aspect = VIEWS[view_id]
    identity = "; ".join(f.value for f in char.identity)
    return (f"{preset.positive}. character reference sheet, single character, "
            f"plain white background, {desc}. {char.name}: {identity}. "
            f"wearing {char.outfits['default']}. {_IDENTITY_ONLY} "
            f"no text, no labels, no watermark.")


def _gen_with_retry(cfg: Config, image: ImageProvider, prompt: str,
                    refs: list[tuple[str, Path]], aspect: str,
                    crop: bool = True) -> bytes:
    last_err = None
    for attempt in range(3):
        try:
            data = image.generate(prompt, refs, aspect, seed=cfg.generation.seed,
                                  crop=crop)
            budget.record(cfg, images=1)
            return data
        except Exception as e:
            last_err = e
            time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"设定卡生图失败：{last_err}")


def _write(cfg: Config, char: Character, style_id: str, view_id: str,
           data: bytes) -> Path:
    out = store.characters_dir(cfg) / char.id / "sheets" / style_id / f"{view_id}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return out


def _boundaries(img, cols: int, rows: int) -> tuple[list[int], list[int]]:
    """投影法找网格间隙：按行/列统计接近白色的像素占比，
    在预期等分位置附近找最白的缝。找不到明显间隙时退回硬性均分。"""
    from PIL import Image

    W0 = 240
    small = img.convert("L").resize((W0, max(1, round(W0 * img.size[1] / img.size[0]))))
    W, H = small.size
    px = small.load()
    row_w = [sum(1 for x in range(W) if px[x, y] > 235) / W for y in range(H)]
    col_w = [sum(1 for y in range(H) if px[x, y] > 235) / H for x in range(W)]

    def pick(profile: list[float], n: int, L: int) -> list[int]:
        bounds = []
        for k in range(1, n):
            center = k * L / n
            band = max(2, int(L / n * 0.10))
            lo, hi = max(0, int(center - band)), min(L, int(center + band) + 1)
            best = max(range(lo, hi), key=lambda i: profile[i])
            # 缝隙不明显（全是颜色或全是白）时退回等分点
            bounds.append(best if profile[best] > 0.5 else int(center))
        return bounds

    xs = [round(x * img.size[0] / W) for x in pick(col_w, cols, W)]
    ys = [round(y * img.size[1] / H) for y in pick(row_w, rows, H)]
    return xs, ys


def slice_grid(data: bytes, cols: int, rows: int) -> list[bytes]:
    """按投影间隙把 cols×rows 网格切成 tiles（行优先）。不预先裁剪比例。"""
    from PIL import Image

    img = Image.open(io.BytesIO(data)).convert("RGB")
    xs, ys = _boundaries(img, cols, rows)
    xs = [0] + xs + [img.size[0]]
    ys = [0] + ys + [img.size[1]]
    tiles = []
    for r in range(rows):
        for c in range(cols):
            tile = img.crop((xs[c], ys[r], xs[c + 1], ys[r + 1]))
            buf = io.BytesIO()
            tile.save(buf, format="PNG")
            tiles.append(buf.getvalue())
    return tiles


def slice_expression_grid(data: bytes) -> list[bytes]:
    """兼容旧单测：一张 3×4 表切成 12 格。新代码用 slice_grid + BUST_SHEETS。"""
    return slice_grid(data, 3, 4)


def _grid_prompt(cfg: Config, char: Character, style_id: str,
                 views: list[tuple[str, str]], cols: int, rows: int) -> str:
    preset = load_presets(cfg)[style_id]
    identity = "; ".join(f.value for f in char.identity)
    exprs = "; ".join(f"{i + 1}: {desc}" for i, (_v, desc) in enumerate(views))
    return (f"{preset.positive}. character expression reference sheet, single character, "
            f"plain white background, a {cols}x{rows} grid of {len(views)} bust portraits "
            f"of the SAME character. Cell order left to right, top to bottom: {exprs}. "
            f"Each cell shows exactly one expression, clearly and distinctly. "
            f"Crisp clean grid, uniform cells with clear gaps between them. "
            f"{char.name}: {identity}. wearing {char.outfits['default']}. "
            f"{_IDENTITY_ONLY} Each cell must show the expression listed for that cell. "
            f"no text, no labels, no watermark.")


def generate_expression_sheets(cfg: Config, image: ImageProvider, char: Character,
                               style_id: str, vision: VisionProvider | None = None,
                               ) -> list[Path]:
    """生成 3 张表情表并切图。vision 给了就盲校验每格标签。"""
    root = store.characters_dir(cfg) / char.id
    src = root / char.source_image
    refs = [("原图（只锁身份，不抄表情）", src)] if src.is_file() else []
    written = []
    for views, cols, rows, aspect in BUST_SHEETS:
        data = _gen_with_retry(cfg, image,
                               _grid_prompt(cfg, char, style_id, views, cols, rows),
                               refs, aspect, crop=False)
        tiles = slice_grid(data, cols, rows)
        for (view_id, _d), tile in zip(views, tiles):
            written.append(_write(cfg, char, style_id, view_id, tile))
    return written


def _verify_cells(cfg: Config, vision: VisionProvider, char: Character,
                  style_id: str) -> None:
    """盲校验：不告诉模型每格该是什么表情，让它自己认，再和标签比对。"""
    from visdiary.schemas import EMOTION_GROUPS, normalize_emotion

    emotions, _ = load_emotions(cfg)
    expected = {p.ref_view: name for name, p in emotions.items() if p.ref_view}
    sheet = char.sheets[style_id]
    base = store.characters_dir(cfg) / char.id
    for view_id in [v for v, _d in BUST_VIEWS]:
        rel = sheet.views.get(view_id)
        want = expected.get(view_id)
        if not rel or not want or not (base / rel).is_file():
            continue
        prompt = (f"这张图是一个角色的表情胸像。从以下情绪里选最像的两个："
                  f"{'、'.join(emotions)}。只按 JSON schema 输出。")
        r = vision.analyze_json(prompt, [base / rel], CellEmotion)
        budget.record(cfg, vision_calls=1)
        got = normalize_emotion(r.detected_emotion)
        ok = got == want or any(want in g and got in g for g in EMOTION_GROUPS)
        sheet.cell_check[view_id] = {"expected": want, "detected": got, "ok": ok}


def generate_sheets(cfg: Config, image: ImageProvider, char: Character,
                    style_id: str, only_view: str | None = None,
                    vision: VisionProvider | None = None) -> list[Path]:
    """为 char 生成 style_id 的设定卡。
    only_view=None：3 张全身 + 3 张表情表；只给 view 时只重画那一张。"""
    if only_view == EXPRESSION_SHEET:
        return generate_expression_sheets(cfg, image, char, style_id, vision)

    root = store.characters_dir(cfg) / char.id
    src = root / char.source_image
    refs = [("原图（只锁身份，不抄表情）", src)] if src.is_file() else []
    written = []
    if only_view is None:
        for view_id, (_desc, aspect) in FULL_VIEWS.items():
            data = _gen_with_retry(cfg, image,
                                   sheet_prompt(cfg, char, style_id, view_id),
                                   refs, aspect)
            written.append(_write(cfg, char, style_id, view_id, data))
        written += generate_expression_sheets(cfg, image, char, style_id, vision)
    else:
        _desc, aspect = VIEWS[only_view]
        data = _gen_with_retry(cfg, image, sheet_prompt(cfg, char, style_id, only_view),
                               refs, aspect)
        written.append(_write(cfg, char, style_id, only_view, data))
    return written


def create_character(cfg: Config, image: ImageProvider, image_path: Path,
                     character_id: str, name: str, analysis: CharacterAnalysis,
                     style_id: str | None = None,
                     vision: VisionProvider | None = None) -> Character:
    root = store.characters_dir(cfg) / character_id
    root.mkdir(parents=True, exist_ok=True)
    src = root / f"source{Path(image_path).suffix.lower() or '.png'}"
    src.write_bytes(Path(image_path).read_bytes())

    presets = load_presets(cfg)
    style = style_id or analysis.style
    if style not in presets:
        style = "anime_cel"

    from visdiary.schemas import Persona
    char = Character(
        id=character_id, name=name, source_image=src.name,
        identity=analysis.identity, outfits={"default": analysis.default_outfit},
        style_detected=analysis.style, style_confidence=analysis.confidence,
        sheets={}, persona=Persona(temperament=analysis.temperament),
    )
    upsert_sheet(cfg, image, char, style, vision=vision)
    return char


def upsert_sheet(cfg: Config, image: ImageProvider, char: Character,
                 style_id: str, only_view: str | None = None,
                 vision: VisionProvider | None = None) -> None:
    generate_sheets(cfg, image, char, style_id, only_view, vision)
    sheet = char.sheets.get(style_id) or StyleSheet(
        style_id=style_id, views={}, approved=False)
    if only_view is None:
        view_ids = list(VIEWS)
    elif only_view == EXPRESSION_SHEET:
        view_ids = [v for v, _d in BUST_VIEWS]
    else:
        view_ids = [only_view]
    for view_id in view_ids:
        sheet.views[view_id] = f"sheets/{style_id}/{view_id}.png"
    # 任何 regen 都重置复核状态，重新 approve 后才能用于 render
    sheet.approved = False
    char.sheets[style_id] = sheet
    if vision and only_view in (None, EXPRESSION_SHEET):
        _verify_cells(cfg, vision, char, style_id)  # 视图路径就位后盲校验
    store.save_character(cfg, char)
    write_review(cfg, char)


def approve_character(cfg: Config, character_id: str, style_id: str) -> Character:
    char = store.load_character(cfg, character_id)
    if style_id not in char.sheets:
        raise ValueError(f"角色 {character_id} 还没有画风 {style_id} 的设定卡")
    char.sheets[style_id].approved = True
    store.save_character(cfg, char)
    return char


_REVIEW_TMPL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>{{ name }} 设定卡</title>
<style>
  body { font-family: sans-serif; max-width: 1200px; margin: 24px auto; }
  .style { margin-bottom: 32px; }
  .grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
  .grid figure { margin: 0; border: 1px solid #ccc; padding: 6px; }
  .grid figure.bad { border: 2px solid #d33; background: #fff0f0; }
  .grid img { width: 100%; display: block; }
  figcaption { font-size: 13px; color: #555; text-align: center; }
  .tag { font-size: 13px; padding: 2px 8px; border-radius: 8px; }
  .ok { background: #d9f2d9; } .no { background: #f2d9d9; }
  .persona { background: #f4f0e8; padding: 12px 16px; border-radius: 8px; }
</style></head><body>
<h1>{{ name }}（{{ cid }}）的角色设定卡</h1>
<p>识别画风：{{ detected }}（置信度 {{ conf }}）· 共 {{ nstyle }} 种画风</p>
{% if temperament %}<div class="persona"><b>人设草稿</b>：{{ temperament }}
<span style="color:#888">（在 character.json 的 persona 里改）</span></div>{% endif %}
{% for sid, sheet in sheets.items() %}
<div class="style">
  <h2>{{ sid }} <span class="tag {{ 'ok' if sheet.approved else 'no' }}">
    {{ '已确认' if sheet.approved else '未确认' }}</span></h2>
  {% if sheet.mismatch %}<p style="color:#d33">以下格子盲校验标签不符，请检查后
    visdiary character regen {{ cid }} --view &lt;view_id&gt; 单张重画</p>{% endif %}
  <div class="grid">
    {% for view, rel in sheet.views.items() %}
    <figure class="{{ 'bad' if sheet.bad.get(view) else '' }}">
      <img src="{{ rel }}">
      <figcaption>{{ view }}{% if sheet.bad.get(view) %}<br>预期 {{ sheet.bad[view] }}
        {% endif %}</figcaption>
    </figure>
    {% endfor %}
  </div>
</div>
{% endfor %}
</body></html>"""


def write_review(cfg: Config, char: Character) -> Path:
    import jinja2

    def sheet_view(s: StyleSheet):
        bad = {v: f"{c['expected']} / 识别 {c['detected']}"
               for v, c in s.cell_check.items() if not c.get("ok")}
        return {"approved": s.approved, "views": s.views, "bad": bad,
                "mismatch": bool(bad)}

    html = jinja2.Template(_REVIEW_TMPL).render(
        name=char.name, cid=char.id, detected=char.style_detected,
        conf=f"{char.style_confidence:.2f}", nstyle=len(char.sheets),
        temperament=char.persona.temperament,
        sheets={sid: sheet_view(s) for sid, s in char.sheets.items()},
    )
    out = store.characters_dir(cfg) / char.id / "review.html"
    out.write_text(html, encoding="utf-8")
    return out
