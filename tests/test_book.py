"""M20 漫画册：scan_pages 过滤、valence、on-this-day 边界、API。"""

import json

import pytest
from fastapi.testclient import TestClient

from visdiary.book import month_pages, on_this_day, scan_pages
from visdiary.config import load_config
from visdiary.schemas import Acting, Panel, Storyboard


def _make_page(root, date, panels=(("开心", 2),), highlight=1):
    d = root / "output" / date
    d.mkdir(parents=True, exist_ok=True)
    ps = [Panel(id=i + 1, shot="近景", scene="s", characters=["demo"],
                weight="highlight" if i + 1 == highlight else "normal",
                acting=Acting(emotion=e, intensity=n),
                action="a", visual_prompt_en="s", source="all")
          for i, (e, n) in enumerate(panels)]
    (d / "storyboard.json").write_text(
        Storyboard(date=date, title=f"t{date}", mood="m", panels=ps).model_dump_json(),
        encoding="utf-8")


class TestScanPages:
    def test_skips_invalid_and_archive(self, _isolated_project):
        root = _isolated_project
        _make_page(root, "2026-10-01")
        _make_page(root, "2026-10-02")
        (root / "output" / "not-a-date").mkdir()
        (root / "output" / "_archive" / "2026-10-03").mkdir(parents=True)
        (root / "output" / "2026-10-04").mkdir()  # 无 storyboard
        pages = scan_pages(load_config())
        assert [p["date"] for p in pages] == ["2026-10-01", "2026-10-02"]

    def test_main_emotion_and_valence(self, _isolated_project):
        root = _isolated_project
        # 开心×2 (valence 2, 强度2) + 哭×1 (valence -2, 强度3)：主情绪开心
        _make_page(root, "2026-10-01",
                   panels=(("开心", 2), ("开心", 1), ("哭", 3)))
        p = scan_pages(load_config())[0]
        assert p["main_emotion"] == "开心"
        # (2*2 + 2*1 + -2*3)/3/3 = (4+2-6)/9 = 0
        assert p["valence"] == 0.0

    def test_valence_all_negative(self, _isolated_project):
        root = _isolated_project
        _make_page(root, "2026-10-01", panels=(("大哭", 3),))
        assert scan_pages(load_config())[0]["valence"] == -2.0  # 大哭3级：效价封顶


class TestOnThisDay:
    def test_leap_year_feb29(self, _isolated_project):
        root = _isolated_project
        _make_page(root, "2027-02-28")
        hits = on_this_day(load_config(), "2028-02-29")  # 真闰日
        labels = [(h["label"], h["date"]) for h in hits]
        assert ("一年前", "2027-02-28") in labels  # 回退到 2/28

    def test_invalid_date_returns_empty(self, _isolated_project):
        assert on_this_day(load_config(), "bad-date") == []

    def test_one_year_ago_found(self, _isolated_project):
        root = _isolated_project
        _make_page(root, "2025-10-16", panels=(("平静", 1),))
        hits = on_this_day(load_config(), "2026-10-16")
        assert any(h["label"] == "一年前" and h["title"] == "t2025-10-16" for h in hits)


class TestBookAPI:
    @pytest.fixture()
    def client(self, _isolated_project):
        _make_page(_isolated_project, "2026-10-01")
        _make_page(_isolated_project, "2026-10-02")
        return TestClient(create_app_safe(), base_url="http://127.0.0.1")

    def test_book_month(self, client):
        r = client.get("/api/book?month=2026-10")
        assert r.status_code == 200
        cells = r.json()
        assert len(cells) == 31                       # 整月日历
        done = [c for c in cells if c["state"] == "done"]
        assert [c["date"] for c in done] == ["2026-10-01", "2026-10-02"]
        assert "main_emotion" in done[0] and "valence" in done[0]

    def test_bad_month_400(self, client):
        assert client.get("/api/book?month=2026-1").status_code == 400

    def test_on_this_day_api(self, client):
        r = client.get("/api/book/on-this-day?date=2026-10-09")
        assert r.status_code == 200 and isinstance(r.json(), list)


def create_app_safe():
    from visdiary.webapp import create_app
    return create_app(load_config())


class TestCharacterOwnership:
    """第八轮：页面归属由 context.json 决定，角色视图不得串页。"""

    def _make_char(self, root, cid):
        # 隔离副本自带 characters/demo，这里补一个最小角色目录
        import shutil
        src = root / "characters" / "demo"
        dst = root / "characters" / cid
        if not dst.exists():
            shutil.copytree(src, dst)
            cj = dst / "character.json"
            cj.write_text(cj.read_text(encoding="utf-8").replace('"demo"', f'"{cid}"'),
                          encoding="utf-8")

    def test_flat_page_belongs_to_owner(self, _isolated_project):
        root = _isolated_project
        _make_page(root, "2026-10-01")
        (root / "output" / "2026-10-01" / "context.json").write_text(
            '{"character_id": "xiaomali"}', encoding="utf-8")
        self._make_char(root, "xiaomali")
        assert [p["date"] for p in scan_pages(load_config(), "xiaomali")] == ["2026-10-01"]
        assert scan_pages(load_config(), "dafeiyu") == []       # 别的角色看不到

    def test_char_dir_page_and_dedupe(self, _isolated_project):
        root = _isolated_project
        self._make_char(root, "xiaomali")
        _make_page(root, "xiaomali/2026-10-05")                   # 新结构 output/<char>/<date>
        _make_page(root, "2026-10-05")                            # 同日平铺旧页
        pages = scan_pages(load_config())
        assert [p["date"] for p in pages] == ["2026-10-05"]
        assert pages[0]["dir"].endswith("xiaomali/2026-10-05"), pages[0]["dir"]  # 子目录版优先

    def test_no_owner_page_only_in_all_view(self, _isolated_project):
        root = _isolated_project
        _make_page(root, "2026-10-01")   # 无 context.json → 无归属
        self._make_char(root, "xiaomali")
        assert len(scan_pages(load_config())) == 1               # 全部视图在
        assert scan_pages(load_config(), "xiaomali") == []       # 角色视图不在

    def test_book_api_character_filter(self, _isolated_project):
        from fastapi.testclient import TestClient
        root = _isolated_project
        self._make_char(root, "xiaomali")
        _make_page(root, "2026-10-01")
        (root / "output" / "2026-10-01" / "context.json").write_text(
            '{"character_id": "xiaomali"}', encoding="utf-8")
        client = TestClient(create_app_safe(), base_url="http://127.0.0.1")
        cells = client.get("/api/book?month=2026-10&character=xiaomali").json()
        done = [c for c in cells if c["state"] == "done"]
        assert [c["date"] for c in done] == ["2026-10-01"]
        assert "character=xiaomali" in done[0]["cover"]           # cover URL 带角色
        cells_b = client.get("/api/book?month=2026-10&character=dafeiyu").json()
        assert not [c for c in cells_b if c["state"] == "done"]

    def test_page_png_rejects_wrong_owner(self, _isolated_project):
        from fastapi.testclient import TestClient
        root = _isolated_project
        self._make_char(root, "xiaomali")
        _make_page(root, "2026-10-01")
        (root / "output" / "2026-10-01" / "context.json").write_text(
            '{"character_id": "xiaomali"}', encoding="utf-8")
        client = TestClient(create_app_safe(), base_url="http://127.0.0.1")
        assert client.get("/api/pages/2026-10-01/page.png?character=xiaomali").status_code == 404  # 无 page.png
        assert client.get("/api/pages/2026-10-01?character=dafeiyu").status_code == 404            # 归属不符
