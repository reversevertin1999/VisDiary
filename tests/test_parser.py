import pytest

from visdiary.diary.parser import parse_diary


@pytest.fixture
def write_diary(tmp_path):
    def _write(name: str, content: str):
        p = tmp_path / name
        p.write_text(content, encoding="utf-8")
        return p
    return _write


def test_frontmatter_fields(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08\nweather: 雨\ntitle: 测试\n---\n正文。\n")
    d = parse_diary(p)
    assert d.date == "2026-10-08"
    assert d.weather == "雨"
    assert d.title == "测试"


def test_split_by_time_headers(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08\n---\n"
                           "## 09:30\n早上训练崩了。\n\n## 21:00\n晚上吃了火锅。\n")
    d = parse_diary(p)
    assert [e.time for e in d.entries] == ["09:30", "21:00"]
    assert "火锅" in d.entries[1].text
    assert all(e.chars > 0 for e in d.entries)


def test_no_time_header_single_entry(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08\n---\n一整天都在改 bug。\n")
    d = parse_diary(p)
    assert len(d.entries) == 1
    assert d.entries[0].time == "all"


def test_preamble_becomes_all_entry(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08\n---\n总起一段。\n## 10:00\n上午的事。\n")
    d = parse_diary(p)
    assert [e.time for e in d.entries] == ["all", "10:00"]


def test_date_fallback_to_filename(write_diary):
    p = write_diary("2026-10-08.md", "没有 frontmatter 的日记。\n")
    d = parse_diary(p)
    assert d.date == "2026-10-08"


def test_datetime_date_normalized(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08 21:00:00\n---\n正文。\n")
    d = parse_diary(p)
    assert d.date == "2026-10-08"  # 不能带冒号，否则 Windows 下 output/<日期>/ 建不了


def test_bad_date_format_raises(write_diary):
    p = write_diary("a.md", "---\ndate: 十月八号\n---\n正文。\n")
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        parse_diary(p)


def test_chinese_colon_and_label(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08\n---\n"
                           "## 09：30 早餐\n吃了包子。\n## 9:45\n去上课。\n")
    d = parse_diary(p)
    assert [(e.time, e.label) for e in d.entries] == [("09:30", "早餐"), ("09:45", None)]
    assert "早餐" not in d.entries[0].text


def test_missing_date_raises(write_diary):
    p = write_diary("diary.md", "既没有 frontmatter 也没有文件名日期。\n")
    with pytest.raises(ValueError):
        parse_diary(p)


def test_char_count_ignores_whitespace(write_diary):
    p = write_diary("a.md", "---\ndate: 2026-10-08\n---\n一二 三四\n\n")
    d = parse_diary(p)
    assert d.char_count == 4
