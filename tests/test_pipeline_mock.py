"""mock 模式端到端：一篇日记跑完整条链路，输出文件齐全。"""

import json

import pytest
from typer.testing import CliRunner

from visdiary.cli import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _no_budget_limit(monkeypatch):
    """端到端测试不碰真实月度预算（真实任务可能正在后台消耗额度）。"""
    from visdiary import budget
    monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))


def test_pipeline_mock(project_tmp):
    outdir = project_tmp / "out"
    result = runner.invoke(app, [
        "render", "diaries/samples/short.md", "--character", "demo",
        "--provider", "mock", "--out", str(outdir),
    ])
    assert result.exit_code == 0, result.output

    # 输出文件齐全
    for rel in ["storyboard.json", "storyboard.warnings.json", "layout.json",
                "qc_report.json", "page.html", "page.png"]:
        assert (outdir / rel).is_file(), rel
    sb = json.loads((outdir / "storyboard.json").read_text(encoding="utf-8"))
    for p in sb["panels"]:
        assert (outdir / "panels" / f"{p['id']:02d}.png").is_file()
        meta = json.loads((outdir / "panels" / f"{p['id']:02d}.meta.json")
                          .read_text(encoding="utf-8"))
        for field in ["prompt", "refs", "provider", "model", "aspect", "qc"]:
            assert field in meta
    report = json.loads((outdir / "qc_report.json").read_text(encoding="utf-8"))
    assert 0 <= report["final_pass_rate"] <= 1
    assert len(report["panels"]) == len(sb["panels"])


def test_second_run_hits_cache(project_tmp):
    outdir = project_tmp / "out"
    args = ["render", "diaries/samples/short.md", "--character", "demo",
            "--provider", "mock", "--out", str(outdir)]
    assert runner.invoke(app, args).exit_code == 0
    # 第二次跑：日记未变 → fast path 直接返回已有页面（0 生图）
    r2 = runner.invoke(app, args)
    assert r2.exit_code == 0
    assert "日记与配置未变" in r2.output
    # --reuse 显式重跑（重新生图走缓存键）
    r3 = runner.invoke(app, args + ["--reuse"])
    assert r3.exit_code == 0
    assert "日记与配置未变" not in r3.output
