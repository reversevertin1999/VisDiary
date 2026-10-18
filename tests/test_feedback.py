"""M16 偏好学习：反馈记录、few-shot 注入、强度校准建议。"""

import pytest

from visdiary.character import feedback as fb
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.diary.parser import parse_diary
from visdiary.providers.mock import MockLLM
from visdiary.storyboard.builder import build_storyboard

CFG = load_config()
CHAR_ID = "demo"
FILE = CFG.path("characters") / CHAR_ID / "feedback.jsonl"


@pytest.fixture(autouse=True)
def clean_feedback():
    FILE.unlink(missing_ok=True)
    yield
    FILE.unlink(missing_ok=True)


def _log(emotion, sys_i, user_i, n=2):
    for k in range(n):
        fb.log_feedback(CFG, CHAR_ID, {
            "date": f"2026-10-{k + 1:02d}", "panel": 1, "text": "测试",
            "system": {"emotion": emotion, "intensity": sys_i},
            "user": {"emotion": emotion, "intensity": user_i, "note": "收敛点"},
        })


def test_log_and_load():
    _log("大笑", 3, 2, n=3)
    rows = fb.load_feedback(CFG, CHAR_ID)
    assert len(rows) == 3 and rows[0]["user"]["intensity"] == 2


def test_find_similar_format():
    _log("大笑", 3, 2, n=2)
    lines = fb.find_similar(CFG, CHAR_ID, text="今天大笑")
    assert lines and "大笑" in lines[0] and "2 级" in lines[0]


def test_calibration_suggestions():
    _log("大笑", 3, 2, n=2)
    _log("哭", 2, 2, n=2)  # 没被下调，不应建议
    sugg = fb.calibration_suggestions(CFG, CHAR_ID)
    assert sugg == {"大笑": 2}


def test_fewshot_calms_mock_storyboard(project_tmp):
    """被纠正过的情绪再次出现时，mock 输出与用户修正一致。"""
    d = project_tmp / "2026-10-08.md"
    d.write_text("---\ndate: 2026-10-08\n---\n## 21:00\n" + "开心的事。" * 30 + "\n",
                 encoding="utf-8")
    diary = parse_diary(d)
    char = load_character(CFG, CHAR_ID)

    sb1, _ = build_storyboard(diary, char, [CHAR_ID], CFG, MockLLM())
    hi1 = next(p for p in sb1.panels if p.weight == "highlight")
    assert hi1.acting.emotion == "大笑" and hi1.acting.intensity == 3  # 默认满级

    _log("大笑", 3, 2, n=3)
    sb2, _ = build_storyboard(diary, char, [CHAR_ID], CFG, MockLLM())
    hi2 = next(p for p in sb2.panels if p.weight == "highlight")
    assert hi2.acting.emotion == "大笑" and hi2.acting.intensity == 2  # 收敛了
