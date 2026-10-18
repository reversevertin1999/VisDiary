"""M15 单格导演：--reuse / edit / redraw / pick / 导演注释。"""

import json
from pathlib import Path

import pytest

from visdiary import budget
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.diary.parser import parse_diary
from visdiary.director import edit_panel, pick_variant, redraw_panel
from visdiary.pipeline import run_pipeline
from visdiary.render import generator
from visdiary.schemas import Storyboard
from visdiary.storyboard.builder import build_storyboard
from visdiary.providers.mock import MockLLM


@pytest.fixture(autouse=True)
def _no_budget_limit(monkeypatch):
    """测试不碰真实月度预算（真实任务可能在后台消耗额度）。"""
    monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))



def _render(project_tmp, name="r"):
    cfg = load_config()
    return cfg, run_pipeline(cfg, Path("diaries/samples/short.md"), "demo",
                             outdir=project_tmp / name)


def _png_bytes(outdir, pid):
    return (outdir / "panels" / f"{pid:02d}.png").read_bytes()


def test_reuse_zero_llm_and_locked_panels_kept(project_tmp):
    cfg, r = _render(project_tmp)
    outdir = r["outdir"]
    # 锁定第 1 格
    metas = {m["panel"]: m for m in
             (json.loads(p.read_text()) for p in (outdir / "panels").glob("*.meta.json"))}
    metas[1]["locked"] = True
    generator.save_meta(outdir, metas[1])
    before_img = _png_bytes(outdir, 1)
    before_usage = budget.usage(cfg)["llm_calls"]

    r2 = run_pipeline(cfg, Path("diaries/samples/short.md"), "demo",
                      outdir=outdir, reuse=True)

    assert budget.usage(cfg)["llm_calls"] == before_usage  # 0 次 LLM 调用
    assert _png_bytes(outdir, 1) == before_img                  # 锁定格原图不动
    assert r2["metas"][1].get("_reused") == "locked"


def test_edit_then_redraw_only_touches_one_panel(project_tmp):
    cfg, r = _render(project_tmp)
    outdir = r["outdir"]
    others = {pid: _png_bytes(outdir, pid) for pid in (2, 3, 4)}

    changed = edit_panel(cfg, outdir, 1, emotion="委屈", intensity=2,
                         note="忍着不哭，眼睛看向窗外")
    assert changed
    sb = Storyboard.model_validate_json((outdir / "storyboard.json").read_text())
    assert sb.panels[0].acting.emotion == "委屈"
    assert sb.panels[0].acting.intensity == 2
    assert "忍着不哭" in sb.panels[0].acting.face_en

    results = redraw_panel(cfg, outdir, 1, variants=2)
    assert len(results) == 2
    # 其他格的文件哈希不变
    for pid, b in others.items():
        assert _png_bytes(outdir, pid) == b

    page = pick_variant(cfg, outdir, 1, "b")
    assert page.is_file()
    meta = json.loads((outdir / "panels" / "01.meta.json").read_text())
    assert meta["locked"] is True and meta["accepted_key"]


def test_directive_enforced(project_tmp):
    d = project_tmp / "2026-10-08.md"
    d.write_text("---\ndate: 2026-10-08\n---\n"
                 "## 09:30\n早上训练崩了。\n"
                 "## 21:00\n晚上吃火锅。\n<!-- vd: 委屈 2, 不要大哭, 忍着不哭 -->\n",
                 encoding="utf-8")
    cfg = load_config()
    diary = parse_diary(d)
    assert diary.entries[1].directive["emotion"] == "委屈"
    assert diary.entries[1].directive["intensity"] == 2
    assert diary.entries[1].directive["ban"] == ["大哭"]
    assert "忍着不哭" in diary.entries[1].directive["note"]

    char = load_character(cfg, "demo")
    sb, _ = build_storyboard(diary, char, ["demo"], cfg, MockLLM())
    for p in sb.panels:
        if p.source == "21:00":
            assert p.acting.emotion == "委屈"      # 导演提示 100% 生效
            assert p.acting.intensity == 2
            assert "忍着不哭" in p.acting.face_en


def test_highlight_variants(project_tmp):
    cfg = load_config()
    cfg.qc.highlight_variants = 2
    r = run_pipeline(cfg, Path("diaries/samples/short.md"), "demo",
                     outdir=project_tmp / "hv")
    hl = next(p["id"] for p in json.loads((r["outdir"] / "storyboard.json").read_text())["panels"]
              if p["weight"] == "highlight")
    meta = json.loads((r["outdir"] / "panels" / f"{hl:02d}.meta.json").read_text())
    assert r["qc"]["highlight_variants"] == 2
    assert "variants" in meta  # 另一张候选留着可 pick
