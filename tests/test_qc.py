"""质检循环：必挂的 vision 会触发重画并标记 flagged；必过的直接通过。

fake vision 会完整覆盖角色的所有特征（第四轮覆盖检查要求），并如实上报
images_received / judgeable。
"""

from pathlib import Path

import pytest

from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.layout.planner import plan_layout
from visdiary.qc.checker import qc_all
from visdiary.render import generator
from visdiary.schemas import QCCheck, QCResult, Storyboard, Panel

@pytest.fixture(scope="module")
def env():
    """延迟到 fixture 阶段构造（隔离 root 已生效）。"""
    cfg = load_config()
    return cfg, load_character(cfg, "demo")


CFG = None
CHAR = None


def _init():
    global CFG, CHAR
    if CFG is None:
        CFG = load_config()
        CHAR = load_character(CFG, "demo")


class AlwaysFailVision:
    name = "fail"

    def analyze_json(self, prompt, images, schema, context=None):
        checks = [QCCheck(name=f.name, feature_id=i, result="fail",
                          note="wrong character")
                  for i, f in enumerate(CHAR.identity, 1)]
        return QCResult(images_received=len(images), judgeable=True,
                        character_visible=True, has_text=False, checks=checks)


class AlwaysPassVision:
    name = "pass"

    def analyze_json(self, prompt, images, schema, context=None):
        checks = [QCCheck(name=f.name, feature_id=i, result="pass")
                  for i, f in enumerate(CHAR.identity, 1)]
        return QCResult(images_received=len(images), judgeable=True,
                        character_visible=True, has_text=False, checks=checks)


def _setup(project_tmp):
    from visdiary.providers.mock import MockImage
    panels = [Panel(id=1, shot="中景", scene="s", characters=["demo"],
                    acting={"emotion": "平静", "intensity": 1}, action="a",
                    visual_prompt_en="s", source="all"),
              Panel(id=2, shot="近景", scene="s", characters=["demo"],
                    acting={"emotion": "开心", "intensity": 2}, action="a",
                    visual_prompt_en="s", source="all")]
    sb = Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)
    layout = plan_layout(sb, CFG.layout)
    outdir = project_tmp / "qc"
    outdir.mkdir(parents=True, exist_ok=True)
    metas = generator.generate_all(CFG, MockImage(), CHAR, sb, layout, "anime_cel", outdir)
    return sb, outdir, metas


def test_failing_qc_redraws_and_flags(project_tmp):
    _init()
    from visdiary.providers.mock import MockImage
    sb, outdir, metas = _setup(project_tmp)
    report = qc_all(CFG, AlwaysFailVision(), MockImage(), CHAR, sb, "anime_cel",
                    outdir, metas)
    assert report["flagged"] == [1, 2]                       # 重画满 2 次仍不合格
    assert all(p["retries"] == CFG.qc.max_retries for p in report["panels"])
    assert report["final_pass_rate"] == 0
    assert report["qc_error_count"] == 0
    assert (outdir / "qc_report.json").is_file()


def test_passing_qc_no_redraw(project_tmp):
    _init()
    sb, outdir, metas = _setup(project_tmp)
    report = qc_all(CFG, AlwaysPassVision(), None, CHAR, sb, "anime_cel", outdir, metas)
    assert report["flagged"] == []
    assert report["first_pass_rate"] == 1.0
    assert all(p["retries"] == 0 for p in report["panels"])
