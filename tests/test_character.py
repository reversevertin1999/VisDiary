"""resolve_style：识别画风与已生成设定卡不一致时的回退逻辑。"""

import pytest

from visdiary.character.builder import resolve_style
from visdiary.schemas import Character, StyleSheet


def make_char(detected="chibi", sheets=("anime_cel",)) -> Character:
    return Character(
        id="c", name="n", source_image="s.png", identity=[], style_detected=detected,
        style_confidence=0.9,
        sheets={s: StyleSheet(style_id=s, views={}) for s in sheets},
    )


def test_explicit_style_wins():
    assert resolve_style(make_char(), "watercolor") == "watercolor"


def test_detected_style_when_sheet_exists():
    assert resolve_style(make_char(detected="anime_cel")) == "anime_cel"


def test_falls_back_to_only_sheet():
    # 识别是 chibi，但只生成过 anime_cel 一套 —— 用那一套
    assert resolve_style(make_char(detected="chibi", sheets=("anime_cel",))) == "anime_cel"


def test_multiple_sheets_without_match_raises():
    with pytest.raises(ValueError, match="--style"):
        resolve_style(make_char(detected="chibi", sheets=("anime_cel", "watercolor")))


def test_no_sheets_returns_detected():
    assert resolve_style(make_char(detected="chibi", sheets=())) == "chibi"
