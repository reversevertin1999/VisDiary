"""M18 网页版 API 测试（mock provider，FastAPI TestClient）。"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from visdiary import budget
from visdiary.config import load_config
from visdiary.webapp import create_app


@pytest.fixture(autouse=True)
def _no_budget_limit(monkeypatch):
    monkeypatch.setattr(budget, "clamp_panel_count", lambda cfg, n: (n, None))
    yield
    # 清理测试产生的日记（render 的输出沿用 2026-10-08 正常产物目录）
    from visdiary.config import load_config
    d = load_config().path("diaries") / "2099-01-02.md"
    d.unlink(missing_ok=True)


H = {"Origin": "http://127.0.0.1", "X-VisDiary": "1"}


@pytest.fixture(scope="module")
def client():
    cfg = load_config()  # 默认 mock provider
    # base_url 指到 127.0.0.1，过 TrustedHost；写请求的头由各测试自己带（H）
    return TestClient(create_app(cfg), base_url="http://127.0.0.1")


def _wait_job(client, job_id, timeout=120):
    import json as _json
    deadline = time.time() + timeout
    while time.time() < deadline:
        # 借 SSE 接口拿历史（连接即返回全部历史 + 结束标记，帧是 JSON）
        with client.stream("GET", f"/api/jobs/{job_id}/events") as r:
            done = False
            for line in r.iter_lines():
                if not line.startswith("data:"):
                    continue
                payload = _json.loads(line[5:].strip())
                if "end" in payload:
                    done = True
            if done:
                return
        time.sleep(0.5)
    raise TimeoutError("job 没跑完")


def test_index_and_diaries(client):
    r = client.get("/")
    assert r.status_code == 200 and "VisDiary" in r.text
    ds = client.get("/api/diaries").json()
    assert any(d["date"] == "2026-10-08" for d in ds)  # short.md 的 frontmatter 日期


def test_save_and_list_diary(client):
    r = client.put("/api/diaries/2099-01-02", json={"content": "---\ndate: 2099-01-02\n---\n测试\n"}, headers=H)
    assert r.status_code == 200
    assert any(d["date"] == "2099-01-02" for d in client.get("/api/diaries").json())


def test_render_job_and_page(client):
    # render 按日期找 diaries/<date>.md；先保存一篇（复制 short 的内容）
    content = open("diaries/samples/short.md", encoding="utf-8").read()
    client.put("/api/diaries/2026-10-08", json={"content": content}, headers=H)
    r = client.post("/api/render/2026-10-08?character=demo", headers=H)
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    _wait_job(client, job_id)  # 结束即通过（SSE 帧见 _wait_job 内解析）

    page = client.get("/api/pages/2026-10-08?character=demo").json()
    assert page["storyboard"]["panels"]
    assert page["layout"]["panels"]
    assert client.get("/api/pages/2026-10-08/page.png?character=demo").status_code == 200
    assert client.get("/api/pages/2026-10-08/panels/1.png?character=demo").status_code == 200


def test_panel_edit_redraw_pick(client):
    # 先保证有产物
    test_render_job_and_page(client)
    r = client.post("/api/pages/2026-10-08/panels/1/edit?character=demo", headers=H,
                    json={"emotion": "委屈", "intensity": 2, "note": "测试备注"})
    assert r.status_code == 200
    job = client.post("/api/pages/2026-10-08/panels/1/redraw?variants=2&character=demo", headers=H).json()
    _wait_job(client, job["job_id"])
    page = client.get("/api/pages/2026-10-08?character=demo").json()
    assert page["storyboard"]["panels"][0]["acting"]["emotion"] == "委屈"

    r = client.post("/api/pages/2026-10-08/panels/1/pick?variant=a&character=demo", headers=H)
    assert r.status_code == 200
    _wait_job(client, r.json()["job_id"])  # pick 走 runner 串行
    page = client.get("/api/pages/2026-10-08?character=demo").json()
    assert page["metas"]["1"]["locked"] is True
    assert client.get("/api/pages/2026-10-08/panels/1.png?character=demo").status_code == 200


def test_characters_and_budget(client):
    cs = client.get("/api/characters").json()
    assert any(c["id"] == "demo" for c in cs)
    d = client.get("/api/characters/demo").json()
    assert "sheets" in d and d["sheets"]["anime_cel"]["views"]
    img = client.get("/api/characters/demo/img?rel=sheets/anime_cel/front_full.png")
    assert img.status_code == 200
    # 越权路径要挡住
    bad = client.get("/api/characters/demo/img?rel=../../character.json")
    assert bad.status_code == 404
    u = client.get("/api/budget").json()
    assert {"prod", "dev", "prod_remaining"} <= set(u)
