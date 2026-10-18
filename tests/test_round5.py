"""第五轮验收测试：网页安全 / SSE 竞态 / edit 校验 / feedback 条件写入 / n/a 分母。"""

import json
import threading
import time

import pytest
from fastapi.testclient import TestClient

from visdiary import budget
from visdiary.config import load_config
from visdiary.director import edit_panel
from visdiary.webapp import Job, create_app

H = {"Origin": "http://127.0.0.1", "X-VisDiary": "1"}


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(load_config()), base_url="http://127.0.0.1")


class TestWebSecurity:
    def test_write_without_origin_403(self, client):
        r = client.post("/api/render/2026-10-08?character=demo",
                        headers={"X-VisDiary": "1"})
        assert r.status_code == 403

    def test_write_with_foreign_origin_403(self, client):
        r = client.post("/api/render/2026-10-08?character=demo",
                        headers={"Origin": "http://evil.example",
                                 "X-VisDiary": "1"})
        assert r.status_code == 403

    def test_write_without_custom_header_403(self, client):
        # 同源但没带 X-VisDiary（网页 CSRF 发不出这个自定义头）
        r = client.post("/api/render/2026-10-08?character=demo",
                        headers={"Origin": "http://127.0.0.1"})
        assert r.status_code == 403

    def test_bad_host_400(self):
        c = TestClient(create_app(load_config()), base_url="http://evil.example")
        assert c.get("/").status_code == 400  # TrustedHost 拦截

    def test_bad_date_rejected(self, client):
        r = client.post("/api/render/../../etc?character=demo", headers=H)
        assert r.status_code in (400, 404)

    def test_bad_cid_rejected(self, client):
        r = client.get("/api/characters/..%2F..%2Fetc")
        assert r.status_code in (400, 404)  # 被拒绝即可（路由层 404 / 校验层 400）

    def test_char_img_prefix_bypass_blocked(self, client):
        # demo2 的图不能通过 demo 前缀读到（_check_cid 挡住 ..；is_relative_to 挡穿越）
        assert client.get("/api/characters/demo/img?rel=../demo2/source.png").status_code == 404
        assert client.get("/api/characters/demo/img?rel=sheets/anime_cel/front_full.png").status_code == 200

    def test_variant_letter_whitelist(self, client):
        assert client.get("/api/pages/2026-10-08/panels/1/variants/..png").status_code in (400, 404)


class TestSSERace:
    def test_finish_during_subscribe_terminates(self):
        """finish 恰好发生在订阅前后：SSE 流必须能结束（P1-7）。"""
        job = Job("t1", "test")
        collected: list[str] = []

        def consume():
            q = __import__("queue").Queue()
            with job.lock:
                history = list(job.steps)
                status = job.status
                if status in ("queued", "running"):
                    job.subs.append(q)
            for line in history:
                collected.append(line)
            if status in ("done", "failed"):
                collected.append(f"__{status}__")
                return
            while True:
                item = q.get()
                if item is None:
                    collected.append(f"__{job.status}__")
                    return
                collected.append(item)

        t = threading.Thread(target=consume)
        t.start()
        time.sleep(0.05)
        job.push("步骤1")
        job.finish("done", {"ok": 1})  # 竞态点：消费线程可能正在订阅
        t.join(timeout=5)
        assert not t.is_alive()          # 没有挂死
        assert collected[-1] == "__done__"


class TestEditPanelValidation:
    def _pack(self, project_tmp):
        from visdiary.pipeline import run_pipeline
        cfg = load_config()
        r = run_pipeline(cfg, "diaries/samples/short.md", "demo",
                         outdir=project_tmp / "edit")
        return cfg, r["outdir"]

    def test_reject_unknown_emotion(self, project_tmp, monkeypatch):
        monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))
        cfg, outdir = self._pack(project_tmp)
        with pytest.raises(ValueError, match="未知情绪"):
            edit_panel(cfg, outdir, 1, "不存在的情绪", None, None)

    def test_reject_bad_intensity(self, project_tmp, monkeypatch):
        monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))
        cfg, outdir = self._pack(project_tmp)
        with pytest.raises(ValueError, match="intensity"):
            edit_panel(cfg, outdir, 1, None, 9, None)

    def test_no_change_no_feedback(self, project_tmp, monkeypatch):
        """值没变（只重画场景）不写 feedback（P0-3）。"""
        from visdiary.character.feedback import load_feedback
        monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))
        cfg, outdir = self._pack(project_tmp)
        f = cfg.path("characters") / "demo" / "feedback.jsonl"
        f.unlink(missing_ok=True)
        sb = json.loads((outdir / "storyboard.json").read_text(encoding="utf-8"))
        cur = sb["panels"][0]["acting"]
        changed = edit_panel(cfg, outdir, 1, cur["emotion"], cur["intensity"], None)
        assert changed == []                       # 值没变：changed 为空
        assert not f.exists() or load_feedback(cfg, "demo") == []  # 没写 feedback

    def test_change_writes_feedback(self, project_tmp, monkeypatch):
        from visdiary.character.feedback import load_feedback
        monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))
        cfg, outdir = self._pack(project_tmp)
        f = cfg.path("characters") / "demo" / "feedback.jsonl"
        f.unlink(missing_ok=True)
        edit_panel(cfg, outdir, 1, "委屈", 2, "测试")
        rows = load_feedback(cfg, "demo")
        assert rows and rows[-1]["user"]["emotion"] == "委屈"  # 真改动才记录


class TestNADenominator:
    def test_na_excluded_from_rates(self, project_tmp, monkeypatch):
        """n/a 的格不计入表情命中率分母（P1-1）。"""
        from visdiary.providers.mock import MockImage, MockVision
        from visdiary.qc.checker import qc_all
        from visdiary.layout.planner import plan_layout
        from visdiary.render import generator
        from visdiary.schemas import Panel, Storyboard, QCResult, ExpressionCheck

        class HalfNaVision(MockVision):
            """主角始终不可见 → 每格都判 n/a。"""
            def analyze_json(self, prompt, images, schema, context=None):
                r = super().analyze_json(prompt, images, schema, context)
                r.character_visible = False  # → n/a
                return r

        cfg = load_config()
        char = cfg and __import__("visdiary.character.store",
                                  fromlist=["load_character"]).load_character(cfg, "demo")
        panels = [Panel(id=1, shot="中景", scene="s", characters=["demo"],
                        acting={"emotion": "开心", "intensity": 2}, action="a",
                        visual_prompt_en="s", source="all"),
                  Panel(id=2, shot="近景", scene="s", characters=["demo"],
                        acting={"emotion": "难过", "intensity": 2}, action="a",
                        visual_prompt_en="s", source="all")]
        sb = Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)
        layout = plan_layout(sb, cfg.layout)
        outdir = project_tmp / "na"
        outdir.mkdir(parents=True)
        monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))
        metas = generator.generate_all(cfg, MockImage(), char, sb, layout,
                                       "anime_cel", outdir)
        vision = HalfNaVision()
        report = qc_all(cfg, vision, MockImage(), char, sb, "anime_cel", outdir, metas)
        assert report["expression_na_count"] == len(panels)  # 全部 n/a
        # 表情命中率分母为 0 → None，而不是把 n/a 算成未命中
        assert report["expression_hit_rate"] is None
        denom = sum(1 for e in report["panels"]
                    if e.get("expression_pass") is not None)
        assert denom == 0
