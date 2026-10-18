"""主链路：分镜 → 预算 → 版面 → 逐格生图 → 质检 → 排版导出。cli 和 bench 共用。

reuse=True 时不重新拆分镜（0 次 LLM 调用），直接读 output/<日期>/storyboard.json，
锁定格和已接受格都复用（第三轮 P1-11）。
"""

import json
from pathlib import Path

from visdiary import budget, providers
from visdiary.character.builder import resolve_style
from visdiary.character.store import list_character_ids, load_character
from visdiary.config import Config
from visdiary.diary.parser import parse_diary
from visdiary.layout.export import export_png
from visdiary.layout.page import build_page
from visdiary.layout.planner import plan_layout
from visdiary.qc.checker import generate_and_qc_all, qc_all
from visdiary.render import generator
from visdiary.schemas import Storyboard
from visdiary.storyboard import rules
from visdiary.storyboard.builder import build_storyboard


def _load_metas(outdir: Path) -> dict[int, dict]:
    return {m["panel"]: m for m in
            (json.loads(p.read_text(encoding="utf-8"))
             for p in sorted((outdir / "panels").glob("*.meta.json")))}


def _fingerprint(cfg: Config, diary_path: Path, char_id: str, style_id: str) -> str:
    """P0-5：日记 + 角色 + 情绪词表 + 关键配置的内容指纹。"""
    import hashlib
    from visdiary.character.store import characters_dir
    h = hashlib.sha256(Path(diary_path).read_bytes())
    for p in (characters_dir(cfg) / char_id / "character.json",
              cfg.path("styles/emotions.yaml")):
        if p.is_file():
            h.update(p.read_bytes())
    h.update(json.dumps({"style": style_id, "max_panels": cfg.storyboard.max_panels,
                         "image": cfg.provider.image}).encode())
    return h.hexdigest()


def run_pipeline(cfg: Config, diary_path: Path, character_id: str,
                 style_id: str | None = None, outdir: Path | None = None,
                 reuse: bool = False, progress=None, force: bool = False) -> dict:
    import time as _t
    t_total = _t.monotonic()
    timing: dict = {"storyboard_s": 0, "layout_s": 0, "panels": {},
                    "export_s": 0, "total_s": 0, "images": 0, "vision_calls": 0}
    doc = parse_diary(diary_path)
    char = load_character(cfg, character_id)
    style_id = resolve_style(char, style_id)
    sheet = char.sheets.get(style_id)
    if not sheet or not sheet.approved:
        raise ValueError(
            f"角色 {character_id} 的画风 {style_id} 还没有确认的设定卡。"
            f"先 visdiary character add / approve。")
    known_ids = list_character_ids(cfg)
    if char.id not in known_ids:
        known_ids.append(char.id)

    # 按角色分目录：output/<char>/<date>（同一天不同角色互不覆盖）
    outdir = (Path(outdir) if outdir else cfg.path("output") / character_id / doc.date)
    outdir.mkdir(parents=True, exist_ok=True)

    # P0-5 快速路径：内容指纹匹配 + 成品存在 → 秒回；指纹只在导出成功后写入
    sb_file = outdir / "storyboard.json"
    page_png = outdir / "page.png"
    fp_file = outdir / ".fingerprint"
    fp = _fingerprint(cfg, diary_path, character_id, style_id)
    if (not reuse and not force and page_png.is_file() and sb_file.is_file()
            and fp_file.is_file() and fp_file.read_text(encoding="utf-8") == fp):
        sb = Storyboard.model_validate_json(sb_file.read_text(encoding="utf-8"))
        return {"date": doc.date, "title": sb.title,
                "panels": len(sb.panels), "cached": len(sb.panels),
                "warnings": ["日记与配置未变，直接返回已有页面（--force 可强制重生成）"],
                "qc": None, "metas": _load_metas(outdir), "png": page_png,
                "outdir": outdir, "timing": {"total_s": 0, "reused_page": True},
                "fast_path": True}

    if reuse:
        sb_file = outdir / "storyboard.json"
        if not sb_file.is_file():
            raise ValueError(f"--reuse 需要已有分镜：{sb_file} 不存在，先正常跑一次 render")
        sb = Storyboard.model_validate_json(sb_file.read_text(encoding="utf-8"))
        warnings = ["--reuse：复用已有分镜，未重新拆分"]
    else:
        if progress:
            progress("拆分镜")
        t0 = _t.monotonic()
        sb, warnings = build_storyboard(doc, char, known_ids, cfg,
                                        providers.get_llm(cfg))
        timing["storyboard_s"] = round(_t.monotonic() - t0, 1)

        # 预算：预计超支就下调格数（在配置副本上改，不污染全局 cfg）
        allowed, notice = budget.clamp_panel_count(cfg, len(sb.panels))
        if notice:
            warnings.append(notice)
            local = cfg.model_copy(deep=True)
            if allowed < local.storyboard.min_panels:
                warnings.append(f"预算不足，按下限 {local.storyboard.min_panels} 格生成")
                allowed = local.storyboard.min_panels
            local.storyboard.max_panels = allowed
            sb, w2 = rules.validate(sb, local.storyboard, known_ids,
                                    {e.time for e in doc.entries},
                                    persona=char.persona, soft=True)
            warnings += w2

    if progress:
        progress("版面规划")
    t0 = _t.monotonic()
    layout = plan_layout(sb, cfg.layout)
    timing["layout_s"] = round(_t.monotonic() - t0, 2)
    (outdir / "storyboard.json").write_text(sb.model_dump_json(indent=2),
                                            encoding="utf-8")
    (outdir / "storyboard.warnings.json").write_text(
        json.dumps(warnings, ensure_ascii=False, indent=2), encoding="utf-8")
    (outdir / "layout.json").write_text(layout.model_dump_json(indent=2),
                                        encoding="utf-8")
    # 供 director 的 edit/redraw/pick 重建页面用
    (outdir / "context.json").write_text(json.dumps(
        {"character_id": char.id, "style_id": style_id, "weather": doc.weather},
        ensure_ascii=False, indent=2), encoding="utf-8")

    image = providers.get_image(cfg)
    # C2：生图+质检+重画流水线化（每格一条流水线，共用线程池）
    if cfg.qc.enabled:
        metas, report = generate_and_qc_all(
            cfg, image, providers.get_vision(cfg), char, sb, layout, style_id,
            outdir, reuse_metas=_load_metas(outdir) if reuse else None,
            progress=progress)
        report = _highlight_variants(cfg, char, sb, style_id, outdir, metas, report)
        timing["vision_calls"] = sum(
            1 + e.get("retries", 0) for e in report.get("panels", [])
            if not e.get("reused"))
    else:
        metas = generator.generate_all(
            cfg, image, char, sb, layout, style_id, outdir,
            reuse_metas=_load_metas(outdir) if reuse else None, progress=progress)
        report = None
    cached = sum(1 for m in metas.values() if m["cache_hit"])
    for pid, m in metas.items():
        timing["panels"][str(pid)] = {
            "gen_s": m.get("duration_s", 0), "qc_s": 0,
            "retries": m.get("qc_retries", 0)}
    timing["images"] = sum(1 for m in metas.values() if not m["cache_hit"])

    if progress:
        progress("排版导出")
    t0 = _t.monotonic()
    html = build_page(sb, layout, cfg, outdir, char.name, doc.weather)
    png = export_png(html, outdir / "page.png", layout.width)
    timing["export_s"] = round(_t.monotonic() - t0, 1)
    timing["total_s"] = round(_t.monotonic() - t_total, 1)
    (outdir / "timing.json").write_text(
        json.dumps(timing, ensure_ascii=False, indent=2), encoding="utf-8")
    fp_file.write_text(fp, encoding="utf-8")   # P0-5：成功后才记录指纹

    return {"date": doc.date, "title": sb.title, "panels": len(sb.panels),
            "cached": cached, "warnings": warnings, "qc": report,
            "metas": metas, "png": png, "outdir": outdir, "timing": timing}


def _highlight_variants(cfg: Config, char, sb: Storyboard, style_id: str,
                        outdir: Path, metas: dict[int, dict], report: dict) -> dict:
    """高光格出第二张候选：都过质检就取质检分高的，另一张存为 variant 供 pick。"""
    from visdiary.director import _qc_score, redraw_panel
    from visdiary.schemas import QCResult

    n_variants = cfg.qc.highlight_variants
    hl = next((p for p in sb.panels if p.weight == "highlight"), None)
    if n_variants < 2 or not hl or metas[hl.id].get("error"):
        return report
    variants = redraw_panel(cfg, outdir, hl.id, variants=n_variants)
    best = max(variants, key=lambda v: _qc_score(QCResult.model_validate(v["qc"])))
    meta = metas[hl.id]
    meta["variants"] = [v for v in variants if v["variant"] != best["variant"]]
    if best["variant"] != "a":
        # P1-1：走 director.pick_variant（同步 cache/accepted/qc/locked 并清理候选）
        from visdiary.director import pick_variant
        pick_variant(cfg, outdir, hl.id, best["variant"])
        report["panels"] = [
            {**e, "switched_to_variant": best["variant"]} if e["panel"] == hl.id else e
            for e in report["panels"]]
    generator.save_meta(outdir, meta)
    report["highlight_variants"] = len(variants)
    return report
