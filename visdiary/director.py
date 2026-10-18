"""M15 单格导演：复用分镜、改一格、重画出候选、选定锁定。

- meta.json 里 locked=true 的格子重跑直接复用，不生图不质检
- 质检通过的格子记 accepted_key，重跑优先复用缓存里的"已接受图"
- edit 改分镜字段（note 进 face_en，最高优先级）；redraw 出 K 张候选；pick 锁定
"""

import json
import string
from pathlib import Path

from visdiary import providers
from visdiary.character.store import load_character
from visdiary.config import Config
from visdiary.layout.export import export_png
from visdiary.layout.page import build_page
from visdiary.layout.planner import PageLayout
from visdiary.providers.base import ImageProvider
from visdiary.qc.checker import (_front_ref, check_panel, expression_fail_reason,
                                 identity_passes)
from visdiary.render import cache, generator
from visdiary.schemas import QCResult, Storyboard


def load_pack(outdir: Path) -> tuple[Storyboard, dict, dict[int, dict]]:
    outdir = Path(outdir)
    sb = Storyboard.model_validate_json(
        (outdir / "storyboard.json").read_text(encoding="utf-8"))
    ctx = json.loads((outdir / "context.json").read_text(encoding="utf-8"))
    metas = {m["panel"]: m for m in
             (json.loads(p.read_text(encoding="utf-8"))
              for p in sorted((outdir / "panels").glob("*.meta.json")))}
    return sb, ctx, metas


def rebuild_page(cfg: Config, outdir: Path) -> Path:
    """按现有 storyboard/layout/panels 重新排版导出（改格/选定后调用）。"""
    outdir = Path(outdir)
    sb, ctx, _m = load_pack(outdir)
    layout = PageLayout.model_validate_json(
        (outdir / "layout.json").read_text(encoding="utf-8"))
    char = load_character(cfg, ctx["character_id"])
    html = build_page(sb, layout, cfg, outdir, char.name, ctx.get("weather"))
    return export_png(html, outdir / "page.png", layout.width)


def edit_panel(cfg: Config, outdir: Path, panel_id: int, emotion: str | None,
               intensity: int | None, note: str | None) -> list[str]:
    """改分镜里的一格。改动后这一格需要重画（accepted_key 作废）。

    第五轮：emotion 不在词表（含别名）就报错；intensity 只收 1–3；
    只有值真的变了才写 feedback（不再产生"没改也算改"的脏偏好数据）。
    """
    from visdiary.schemas import EMOTIONS, normalize_emotion

    outdir = Path(outdir)
    sb, _ctx, metas = load_pack(outdir)
    panel = next((p for p in sb.panels if p.id == panel_id), None)
    if not panel:
        raise ValueError(f"没有第 {panel_id} 格")
    if emotion is not None:
        emotion = normalize_emotion(emotion)
        if emotion not in EMOTIONS:
            raise ValueError(f"未知情绪 {emotion!r}，候选见 emotions.yaml")
    if intensity is not None and not 1 <= intensity <= 3:
        raise ValueError("intensity 只能是 1/2/3")

    old = {"emotion": panel.acting.emotion, "intensity": panel.acting.intensity}
    changed = []
    if emotion and emotion != panel.acting.emotion:
        panel.acting.emotion = emotion
        changed.append(f"emotion→{emotion}")
    if intensity and intensity != panel.acting.intensity:
        panel.acting.intensity = intensity
        changed.append(f"intensity→{intensity}")
    if note:
        a = panel.acting
        a.face_en = f"{a.face_en}; {note}" if a.face_en else note
        changed.append("note 写入 face_en")
    (outdir / "storyboard.json").write_text(sb.model_dump_json(indent=2),
                                            encoding="utf-8")
    meta = metas.get(panel_id)
    if meta:
        meta["accepted_key"] = None
        meta["locked"] = False
        generator.save_meta(outdir, meta)
    # M16 + A7：只有情绪或强度确实变了才记 feedback（只改 note / 值没变都不算）
    emo_changed = panel.acting.emotion != old["emotion"]
    int_changed = panel.acting.intensity != old["intensity"]
    if meta and (emo_changed or int_changed):
        from visdiary.character.feedback import log_feedback
        ctx = load_pack(outdir)[1]
        log_feedback(cfg, ctx["character_id"], {
            "date": sb.date, "panel": panel_id,
            "text": (panel.narration or panel.scene)[:40],
            "system": {"emotion": old["emotion"], "intensity": old["intensity"]},
            "user": {"emotion": panel.acting.emotion,
                     "intensity": panel.acting.intensity, "note": note or ""},
        })
    return changed


def _qc_score(result: QCResult) -> int:
    """质检分：身份全过得特征数 + 表情过加 1。用于候选二选一。"""
    s = sum(1 for c in result.checks if c.result == "pass")
    if result.expression is not None and result.character_visible and not result.has_text:
        s += 1
    return s


def redraw_panel(cfg: Config, outdir: Path, panel_id: int, variants: int = 2,
                 image: ImageProvider | None = None,
                 progress=None) -> list[dict]:
    """只重画一格，出 variants 张候选（含现有图为候选 a），pick 前正式格保持原图。

    第五轮：P1-5 正式格/原 meta 的恢复放进 try/finally，中途任何失败都不会
    让正式格缺图；P1-6 身份判定按这一格是否真有主角来算。
    """
    import shutil

    outdir = Path(outdir)
    sb, ctx, metas = load_pack(outdir)
    panel = next((p for p in sb.panels if p.id == panel_id), None)
    if not panel:
        raise ValueError(f"没有第 {panel_id} 格")
    image = image or providers.get_image(cfg)
    vision = providers.get_vision(cfg)
    char = load_character(cfg, ctx["character_id"])
    meta = dict(metas[panel_id])  # 复制，失败时不动原 meta
    has_protagonist = char.id in panel.characters

    png = outdir / "panels" / f"{panel_id:02d}.png"
    meta_file = outdir / "panels" / f"{panel_id:02d}.meta.json"
    backup_meta = meta_file.read_bytes() if meta_file.is_file() else None
    front = _front_ref(cfg, char, ctx["style_id"])

    results = []
    try:
        if png.is_file():  # 现有图作为候选 a
            va = outdir / "panels" / f"{panel_id:02d}.variant-a.png"
            shutil.copy2(png, va)
            if progress:
                progress(f"候选 a 质检中")
            r = check_panel(cfg, vision, char, panel, va, front, has_protagonist)
            results.append({"variant": "a", "file": va.name,
                            "cache_key": meta.get("cache_key"),
                            "identity_pass": identity_passes(r, char, has_protagonist, cfg=cfg, panel=panel)[0],
                            "expression_fail": expression_fail_reason(r, panel,
                                                                      has_protagonist),
                            "qc": json.loads(r.model_dump_json())})
        for k in range(1, variants):
            letter = string.ascii_lowercase[k]
            if progress:
                progress(f"候选 {letter} 生图中")
            m = generator.generate_panel(cfg, image, char, panel, meta["aspect"],
                                         ctx["style_id"], sb.extras_map(), outdir,
                                         attempt=k + 1)
            variant_file = outdir / "panels" / f"{panel_id:02d}.variant-{letter}.png"
            png.replace(variant_file)
            if progress:
                progress(f"候选 {letter} 质检中")
            r = check_panel(cfg, vision, char, panel, variant_file, front,
                            has_protagonist)
            results.append({"variant": letter, "file": variant_file.name,
                            "cache_key": m["cache_key"],
                            "identity_pass": identity_passes(r, char, has_protagonist, cfg=cfg, panel=panel)[0],
                            "expression_fail": expression_fail_reason(r, panel,
                                                                      has_protagonist),
                            "qc": json.loads(r.model_dump_json())})
        # 恢复正式格为候选 a（原图）
        if results and results[0]["variant"] == "a":
            shutil.copy2(outdir / "panels" / results[0]["file"], png)
    finally:  # A3：无论成败，正式格缺图就用候选 a（原图）恢复，再恢复 meta
        if not png.is_file():
            va = outdir / "panels" / f"{panel_id:02d}.variant-a.png"
            if va.is_file():
                shutil.copy2(va, png)
            elif backup_meta is not None:
                meta_file.write_bytes(backup_meta)
                raise RuntimeError(f"第 {panel_id} 格重画中断，且无法恢复正式格")
        meta["variants"] = results
        meta["locked"] = False
        generator.save_meta(outdir, meta)
    return results


def pick_variant(cfg: Config, outdir: Path, panel_id: int, variant: str) -> Path:
    """选定候选：覆盖正式格图，锁定，重排整页。"""
    outdir = Path(outdir)
    sb, _ctx, metas = load_pack(outdir)
    meta = metas[panel_id]
    choice = next((v for v in meta.get("variants", []) if v["variant"] == variant), None)
    if not choice:
        raise ValueError(f"第 {panel_id} 格没有候选 {variant!r}（现有："
                         f"{[v['variant'] for v in meta.get('variants', [])]}）")
    src = outdir / "panels" / choice["file"]
    src.replace(outdir / "panels" / f"{panel_id:02d}.png")
    meta["locked"] = True
    meta["accepted_key"] = choice["cache_key"]
    meta["qc"] = choice["qc"]
    meta["flagged"] = False
    generator.save_meta(outdir, meta)
    # 选定后清理其余候选文件
    for v in meta.get("variants", []):
        if v["variant"] != variant:
            (outdir / "panels" / v["file"]).unlink(missing_ok=True)
    meta["variants"] = []
    generator.save_meta(outdir, meta)
    return rebuild_page(cfg, outdir)


def auto_pick_highlight(cfg: Config, outdir: Path, meta: dict,
                        variants: list[dict]) -> dict:
    """高光格多张候选都没人指定时，取质检分高的那张。"""
    best = max(variants, key=lambda v: (_qc_score(QCResult.model_validate(v["qc"]))))
    return best
