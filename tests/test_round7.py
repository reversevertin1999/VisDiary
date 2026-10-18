"""第七轮验收测试：超时生效、最终 meta、指纹失效、edit job 流程。"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visdiary.config import load_config
from visdiary.schemas import QCCheck, QCResult


class TestP02PanelTimeout:
    def test_timeout_stops_redraw(self, _isolated_project):
        """慢生图 + 永远不合格 → deadline 到了就停，retries 有界且 timeout 标记。"""
        from visdiary.layout.planner import plan_layout
        from visdiary.qc.checker import generate_and_qc_all
        from visdiary.render import generator
        from visdiary.schemas import Acting, Panel, Storyboard
        from visdiary.character.store import load_character

        class SlowImage:
            name = "slow"
            model = "slow-0"

            def generate(self, *a, **kw):
                time.sleep(0.5)
                from visdiary.providers.mock import MockImage
                return MockImage().generate(*a, **kw)

        class AlwaysFail:
            name = "fail"

            def analyze_json(self, prompt, images, schema, context=None):
                checks = [QCCheck(name=f.name, feature_id=i, result="fail")
                          for i, f in enumerate(CHAR.identity, 1)]
                return QCResult(images_received=len(images), judgeable=True,
                                character_visible=True, has_text=False,
                                checks=checks)

        cfg = load_config()
        cfg.generation.panel_timeout_s = 1  # 1 秒预算（生图就 0.5s，最多 1 次重画）
        cfg.qc.max_retries = 9              # 名义上可重画 9 次
        char = load_character(cfg, "demo")
        CHAR = char
        panels = [Panel(id=1, shot="近景", scene="s", characters=["demo"],
                        acting=Acting(emotion="开心", intensity=2),
                        action="a", visual_prompt_en="s", source="all")]
        sb = Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)
        layout = plan_layout(sb, cfg.layout)
        outdir = _isolated_project / "output" / "t-out"
        outdir.mkdir(parents=True)
        from visdiary import budget
        import visdiary.qc.checker as C
        orig = C.identity_passes
        C.identity_passes = lambda r, c, h, **kw: (False, ["timeout-test"], [])
        try:
            metas, report = generate_and_qc_all(
                cfg, SlowImage(), AlwaysFail(), char, sb, layout, "anime_cel", outdir)
        finally:
            C.identity_passes = orig
        entry = report["panels"][0]
        assert entry["retries"] <= 2                     # 超时兜住了（不是 9）
        assert metas[1].get("timeout") is True            # P0-2 标记


class TestP03FinalMeta:
    def test_pipeline_returns_redrawn_meta(self, _isolated_project):
        """第一次质检失败 → 重画通过：返回的 meta 必须是重画后的（cache_key 与磁盘一致）。"""
        from visdiary.layout.planner import plan_layout
        from visdiary.qc.checker import generate_and_qc_all
        from visdiary.schemas import Acting, Character, Feature, Panel, Storyboard
        from visdiary.character.store import load_character
        from visdiary.providers.mock import MockImage, MockVision
        import visdiary.qc.checker as C

        cfg = load_config()
        char = load_character(cfg, "demo")
        panels = [Panel(id=1, shot="近景", scene="s", characters=["demo"],
                        acting=Acting(emotion="开心", intensity=2),
                        action="a", visual_prompt_en="s", source="all")]
        sb = Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)
        layout = plan_layout(sb, cfg.layout)
        outdir = _isolated_project / "output" / "t-final"
        outdir.mkdir(parents=True)

        calls = {"n": 0}
        real_passes = C.identity_passes

        def flaky(result, ch, has_p, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return False, ["第一次故意失败"], []
            return real_passes(result, ch, has_p, **kw)

        C.identity_passes = flaky
        try:
            metas, report = generate_and_qc_all(
                cfg, MockImage(), MockVision(), char, sb, layout, "anime_cel",
                outdir, reuse_metas=None)
        finally:
            C.identity_passes = real_passes
        assert report["panels"][0]["retries"] == 1
        disk = json.loads((outdir / "panels" / "01.meta.json").read_text())
        assert metas[1]["cache_key"] == disk["cache_key"]      # P0-3
        assert metas[1].get("accepted_key")                    # 通过后有 accepted


class TestP05Fingerprint:
    def test_fingerprint_invalidated_by_emotions(self, _isolated_project):
        """改 emotions.yaml → 指纹变 → 重新生成（不再秒回）。"""
        from visdiary.pipeline import run_pipeline
        cfg = load_config()
        diary = _isolated_project / "diaries/samples/short.md"
        out = _isolated_project / "output" / "2026-10-08"
        r1 = run_pipeline(cfg, diary, "demo", outdir=out)
        assert not r1.get("fast_path")
        r2 = run_pipeline(cfg, diary, "demo", outdir=out)
        assert r2.get("fast_path")                            # 未变 → 秒回
        yml = _isolated_project / "styles/emotions.yaml"
        yml.write_text(yml.read_text(encoding="utf-8") + "\n# touched\n",
                       encoding="utf-8")
        r3 = run_pipeline(cfg, diary, "demo", outdir=out)
        assert not r3.get("fast_path")                        # 配置变 → 重生成

    def test_force_skips_fast_path(self, _isolated_project):
        from visdiary.pipeline import run_pipeline
        cfg = load_config()
        diary = _isolated_project / "diaries/samples/short.md"
        out = _isolated_project / "output" / "2026-10-08"
        run_pipeline(cfg, diary, "demo", outdir=out)
        r = run_pipeline(cfg, diary, "demo", outdir=out, force=True)
        assert not r.get("fast_path")                         # --force 生效


H = {"Origin": "http://127.0.0.1", "X-VisDiary": "1"}


class TestP01EditFlow:
    def test_edit_job_returns_changed(self, _isolated_project):
        """edit job 完成事件带 result.changed（P0-1 后端侧）。"""
        from visdiary.pipeline import run_pipeline
        from visdiary.webapp import create_app, Runner, Job
        cfg = load_config()
        out = _isolated_project / "output" / "2026-10-08"
        run_pipeline(cfg, _isolated_project / "diaries/samples/short.md",
                     "demo", outdir=out)
        c = TestClient(create_app(cfg), base_url="http://127.0.0.1")
        r = c.post("/api/pages/2026-10-08/panels/1/edit", headers=H,
                   json={"emotion": "委屈", "intensity": 2})
        job_id = r.json()["job_id"]
        # 等 job 结束并收 result
        deadline = time.time() + 30
        result = None
        while time.time() < deadline:
            with c.stream("GET", f"/api/jobs/{job_id}/events") as resp:
                for line in resp.iter_lines():
                    if line.startswith("data:") and '"result"' in line:
                        payload = json.loads(line[5:])
                        if "end" in payload:
                            result = payload
            if result:
                break
            time.sleep(0.3)
        assert result and result["end"] == "done"
        assert "changed" in (result.get("result") or {}), result
