"""用假 LLM 测 builder：不合规输出不崩溃，该修的修，该重试的重试。"""

import pytest
from pydantic import BaseModel, ValidationError

from visdiary.config import load_config
from visdiary.diary.parser import parse_diary
from visdiary.schemas import Dialogue, Panel, Storyboard
from visdiary.storyboard.builder import build_storyboard, suggest_panel_count
from visdiary.storyboard.rules import StoryboardError
from visdiary.character.store import load_character

CFG = load_config()
CHAR = load_character(CFG, "demo")


def make_diary(tmp_path, text="早上训了练，晚上吃了火锅。" * 5):
    p = tmp_path / "2026-10-08.md"
    p.write_text(f"---\ndate: 2026-10-08\n---\n## 09:30\n{text}\n", encoding="utf-8")
    return parse_diary(p)


def dirty_panel(i: int) -> Panel:
    """各种越界，但都能自动修：别名镜头、50 字旁白、4 条对白、未知角色。"""
    return Panel(
        id=i, shot="中近景", scene="场景", characters=["demo", "ghost"],
        expression="沮丧", action="动作", visual_prompt_en="a scene",
        narration="长" * 50,
        dialogue=[Dialogue(speaker="demo", text="话" * 40) for _ in range(4)],
        source="09:30",
    )


class DirtyLLM:
    name = "dirty"

    def chat_json(self, system, user, schema: type[BaseModel], context=None):
        return Storyboard(date="2026-10-08", title="t", mood="m",
                          panels=[dirty_panel(i) for i in range(1, 4)])


class BrokenLLM:
    """输出彻底不合规（schema 直接拒），重试也一样。"""

    name = "broken"
    calls = 0

    def chat_json(self, system, user, schema: type[BaseModel], context=None):
        BrokenLLM.calls += 1
        return Panel(id=1, shot="航拍", scene="s", characters=[], expression="平静",
                     action="a", visual_prompt_en="s", source="all")


class RecoveringLLM:
    """第一次格数不足触发重试，第二次正常。"""

    name = "recovering"
    calls = 0

    def chat_json(self, system, user, schema: type[BaseModel], context=None):
        RecoveringLLM.calls += 1
        if RecoveringLLM.calls == 1:
            return Storyboard(date="2026-10-08", title="t", mood="m",
                              panels=[dirty_panel(1)])
        return Storyboard(date="2026-10-08", title="t", mood="m",
                          panels=[dirty_panel(i) for i in range(1, 4)])


def test_dirty_output_auto_fixed(tmp_path):
    sb, warns = build_storyboard(make_diary(tmp_path), CHAR, ["demo"], CFG, DirtyLLM())
    assert len(sb.panels) == 3
    assert sb.panels[0].shot == "近景"                        # 中近景 -> 近景
    assert all(a.shot != b.shot for a, b in zip(sb.panels, sb.panels[1:]))
    assert all(p.acting.emotion == "难过" for p in sb.panels)  # 沮丧 -> 难过
    assert all(len(p.narration) <= 28 for p in sb.panels)
    assert all(len(p.dialogue) <= 2 for p in sb.panels)
    assert all(p.characters == ["demo"] for p in sb.panels)
    assert warns  # 修复全部记录在案


def test_broken_output_retries_once_then_raises(tmp_path):
    BrokenLLM.calls = 0
    with pytest.raises(StoryboardError, match="重试"):
        build_storyboard(make_diary(tmp_path), CHAR, ["demo"], CFG, BrokenLLM())
    assert BrokenLLM.calls == 2


def test_under_min_retried_and_recovers(tmp_path):
    RecoveringLLM.calls = 0
    sb, _ = build_storyboard(make_diary(tmp_path), CHAR, ["demo"], CFG, RecoveringLLM())
    assert len(sb.panels) == 3
    assert RecoveringLLM.calls == 2


class TestSuggestPanelCount:
    def _diary(self, tmp_path, n_chars, n_entries=1):
        body = "\n".join(f"## {9 + i:02d}:00\n" + "字" * (n_chars // n_entries)
                         for i in range(n_entries))
        p = tmp_path / "2026-10-08.md"
        p.write_text(f"---\ndate: 2026-10-08\n---\n{body}\n", encoding="utf-8")
        return parse_diary(p)

    def test_continuous_growth(self, tmp_path):
        assert suggest_panel_count(self._diary(tmp_path, 100), CFG) == 3
        assert suggest_panel_count(self._diary(tmp_path, 300), CFG) == 6
        assert suggest_panel_count(self._diary(tmp_path, 900), CFG) == 10

    def test_at_least_one_panel_per_entry(self, tmp_path):
        d = self._diary(tmp_path, 20, n_entries=5)  # 100 字但 5 条随笔
        assert suggest_panel_count(d, CFG) >= 5

    def test_clamped_by_config(self, tmp_path):
        assert suggest_panel_count(self._diary(tmp_path, 99999), CFG) == 12
