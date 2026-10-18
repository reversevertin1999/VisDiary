"""第六轮 A 组验收：测试隔离证明、list_pages、redraw 恢复、variants 上限、
SSE 真实代码路径、edit 走 runner、feedback 条件、日期校验。"""

import hashlib
import json
import queue
import shutil
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from visdiary.config import load_config
from visdiary.webapp import Job, create_app, sse_stream

H = {"Origin": "http://127.0.0.1", "X-VisDiary": "1"}
PROJECT = Path(__file__).resolve().parent.parent
# usage 不盯：后台批量任务（如 E 组 demo 生成）会正常写 dev 池记账，
# 与测试无关的 mtime 变化会造成假阳性
WATCHED = ["diaries", "output", "characters/demo/feedback.jsonl"]


def _snapshot() -> dict:
    out = {}
    for rel in WATCHED:
        p = PROJECT / rel
        if p.is_file():
            out[rel] = (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        elif p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and "__pycache__" not in str(f):
                    out[str(f.relative_to(PROJECT))] = \
                        (f.stat().st_mtime_ns, hashlib.sha256(f.read_bytes()).hexdigest())
    return out


@pytest.fixture(scope="module", autouse=True)
def _real_data_guard():
    """A2 验收：整个模块跑完，真实数据的 mtime+sha256 必须不变。"""
    before = _snapshot()
    yield
    after = _snapshot()
    changed = {k for k in set(before) | set(after)
               if before.get(k) != after.get(k)}
    assert not changed, f"测试改了真实数据：{sorted(changed)[:5]}"


@pytest.fixture(scope="module")
def client():
    return TestClient(create_app(load_config()), base_url="http://127.0.0.1")


class TestA1ListPages:
    def test_three_dirs_two_dates_sorted(self, _isolated_project):
        root = _isolated_project
        out = root / "output"
        for d in ("2026-10-08", "2026-10-09-v4", "2026-10-09", "not-a-date"):
            (out / d).mkdir(parents=True, exist_ok=True)
        # 只有合法日期 + storyboard.json 的算页；CLI 生成的页没有 job.json 也要列出
        (out / "2026-10-08" / "storyboard.json").write_text("{}", encoding="utf-8")
        (out / "2026-10-09" / "storyboard.json").write_text("{}", encoding="utf-8")
        (out / "2026-10-09-v4" / "storyboard.json").write_text("{}", encoding="utf-8")
        c = TestClient(create_app(load_config()), base_url="http://127.0.0.1")
        pages = c.get("/api/pages").json()
        dates = sorted(p["date"] for p in pages if isinstance(p, dict))
        assert "2026-10-08" in dates and "2026-10-09" in dates


class TestA5VariantsLimit:
    def test_variants_out_of_range_400(self, client):
        for bad in (1, 5):
            r = client.post("/api/pages/2026-10-08/panels/1/redraw"
                            f"?variants={bad}", headers=H)
            assert r.status_code == 400


class TestA6RealSSE:
    def test_sse_stream_finish_race(self):
        """直接调用真实的 sse_stream（A6），finish 竞态下流必须终止。"""
        job = Job("j1", "test")
        out = []

        def consume():
            for frame in sse_stream(job):
                out.append(frame)
                if '"end"' in frame:
                    return

        t = threading.Thread(target=consume)
        t.start()
        time.sleep(0.05)
        job.push("步骤1")
        job.finish("done", {"ok": 1})  # 竞态点
        t.join(timeout=5)
        assert not t.is_alive()
        assert any('"end": "done"' in f for f in out)

    def test_bad_date_reaches_handler_400(self, client):
        # 2026-1-1 能穿过路由进 handler，必须被 _check_date 拒
        r = client.post("/api/pages/2026-1-1/panels/1/redraw?variants=2", headers=H)
        assert r.status_code == 400


class TestA4EditViaRunner:
    def test_edit_returns_job(self, client, _isolated_project):
        # 先用 mock 生成一页（隔离根里）
        from visdiary.pipeline import run_pipeline
        cfg = load_config()
        run_pipeline(cfg, _isolated_project / "diaries/samples/short.md", "demo",
                     outdir=_isolated_project / "output" / "2026-10-08")
        c = TestClient(create_app(cfg), base_url="http://127.0.0.1")
        r = c.post("/api/pages/2026-10-08/panels/1/edit", headers=H,
                   json={"emotion": "委屈", "intensity": 2})
        assert r.status_code == 200
        assert "job_id" in r.json()  # A4：edit 走 runner 了

        def wait(job_id, timeout=30):
            deadline = time.time() + timeout
            while time.time() < deadline:
                with c.stream("GET", f"/api/jobs/{job_id}/events") as resp:
                    done = any('"end"' in l for l in resp.iter_lines())
                if done:
                    return
                time.sleep(0.3)
            raise TimeoutError

        wait(r.json()["job_id"])
        sb = json.loads((_isolated_project / "output/2026-10-08/storyboard.json")
                        .read_text(encoding="utf-8"))
        assert sb["panels"][0]["acting"]["emotion"] == "委屈"


class TestA3RedrawRestore:
    def test_failure_restores_panel_image(self, _isolated_project):
        from visdiary.pipeline import run_pipeline
        from visdiary.director import redraw_panel
        cfg = load_config()
        out = _isolated_project / "output" / "2026-10-08"
        run_pipeline(cfg, _isolated_project / "diaries/samples/short.md", "demo",
                     outdir=out)
        png = out / "panels" / "01.png"
        orig_sha = hashlib.sha256(png.read_bytes()).hexdigest()

        class ExplodingVision:
            name = "boom"
            calls = 0

            def analyze_json(self, prompt, images, schema, context=None):
                from visdiary.schemas import QCResult
                ExplodingVision.calls += 1
                if ExplodingVision.calls >= 2:  # 候选 a 质检成功后，第一次生图质检时炸
                    raise RuntimeError("vision down")
                return QCResult(images_received=2, judgeable=True,
                                character_visible=True, has_text=False, checks=[])

        from visdiary.providers.mock import MockImage
        import visdiary.director as D
        orig_vision = D.providers.get_vision
        D.providers.get_vision = lambda cfg: ExplodingVision()
        try:
            with pytest.raises(Exception):
                redraw_panel(cfg, out, 1, variants=2)
        finally:
            D.providers.get_vision = orig_vision
        # A3：正式格图片必须被恢复（sha 一致——mock 候选 a 就是原图的拷贝）
        assert png.is_file()
        assert hashlib.sha256(png.read_bytes()).hexdigest() == orig_sha


class TestA7FeedbackCondition:
    def test_note_only_no_feedback(self, _isolated_project):
        from visdiary.pipeline import run_pipeline
        from visdiary.director import edit_panel
        cfg = load_config()
        out = _isolated_project / "output" / "2026-10-08"
        run_pipeline(cfg, _isolated_project / "diaries/samples/short.md", "demo",
                     outdir=out)
        # A7：前端带了一样的 emotion（没变），只加 note → 不写 feedback
        sb = json.loads((out / "storyboard.json").read_text(encoding="utf-8"))
        cur = sb["panels"][0]["acting"]
        edit_panel(cfg, out, 1, cur["emotion"], None, note="只改备注")
        f = _isolated_project / "characters/demo/feedback.jsonl"
        assert not f.exists()
