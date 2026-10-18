import pytest

from visdiary.config import StoryboardConfig
from visdiary.schemas import Acting, Dialogue, Panel, Storyboard
from visdiary.storyboard.rules import (TEXT_LIMIT, EmotionRuleError, StoryboardError,
                                       validate)

CFG = StoryboardConfig(min_panels=2, max_panels=12)
KNOWN = ["demo"]
SOURCES = {"all"}
EMOS = ["开心", "难过", "期待", "生气", "感动", "惊讶", "坚定", "大笑"]


def make_panel(i: int, **kw) -> Panel:
    emo = kw.pop("emotion", EMOS[i % len(EMOS)])
    weight = kw.pop("weight", "normal")
    intensity = kw.pop("intensity", 3 if weight == "highlight" else 2)
    base = dict(
        id=i, shot="中景", scene="场景", characters=["demo"], weight=weight,
        acting=Acting(emotion=emo, intensity=intensity),
        action="动作", visual_prompt_en="a scene", source="all",
    )
    base.update(kw)
    return Panel(**base)


def make_sb(panels) -> Storyboard:
    return Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)


class TestPanelCount:
    def test_over_max_truncated_and_highlight_kept(self):
        panels = [make_panel(i) for i in range(1, 15)]  # 14 > 12
        panels[10].weight = "highlight"                 # 高光格靠后，验证截断时保留
        panels[10].acting.intensity = 3
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert len(sb.panels) == 12
        assert sum(p.weight == "highlight" for p in sb.panels) == 1
        assert any("截断" in w for w in warns)

    def test_under_min_raises(self):
        with pytest.raises(StoryboardError):
            validate(make_sb([make_panel(1)]), CFG, KNOWN, SOURCES)


class TestHighlight:
    def test_no_highlight_picks_strongest_emotion(self):
        panels = [make_panel(1), make_panel(2), make_panel(3)]
        panels[1].dialogue = [Dialogue(speaker="demo", text="一"),
                              Dialogue(speaker="demo", text="二")]
        panels[2].acting.intensity = 3  # 情绪最强，比对白多更优先
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert [p.weight for p in sb.panels] == ["normal", "normal", "highlight"]
        assert sb.panels[2].acting.intensity == 3
        assert any("高光" in w for w in warns)

    def test_auto_highlight_bumped_to_3(self):
        panels = [make_panel(1), make_panel(2)]
        sb, _ = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        hl = next(p for p in sb.panels if p.weight == "highlight")
        assert hl.acting.intensity == 3  # 自动选的高光格必须满足节奏规则

    def test_multiple_highlights_keep_first(self):
        panels = [make_panel(1, weight="highlight"), make_panel(2, weight="highlight")]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert [p.weight for p in sb.panels] == ["highlight", "normal"]
        assert any("高光" in w for w in warns)


class TestShots:
    def test_adjacent_duplicate_fixed_nearest_distance(self):
        panels = [make_panel(1, shot="近景"), make_panel(2, shot="近景"),
                  make_panel(3, shot="全景")]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].shot != sb.panels[1].shot
        assert sb.panels[1].shot != sb.panels[2].shot
        # 近景重复时选最近的景别（特写/中景档），而不是按列表顺序选到远景
        assert sb.panels[1].shot in {"特写", "中景", "近景"}
        assert any("镜头重复" in w for w in warns)

    def test_strong_emotion_locked_to_close_shot(self):
        panels = [make_panel(1, shot="远景", intensity=3), make_panel(2, shot="全景")]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].shot in {"近景", "特写", "仰视"}
        assert any("强情绪" in w or "表情" in w for w in warns)

    def test_highlight_locked_to_close_shot(self):
        panels = [make_panel(1, shot="远景", weight="highlight"), make_panel(2)]
        sb, _ = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].shot in {"近景", "特写", "仰视"}

    def test_back_view_no_strong_expression(self):
        panels = [make_panel(1, shot="背影", intensity=2),
                  make_panel(2, shot="全景", weight="highlight")]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].shot == "中景"
        assert any("背影" in w for w in warns)

    def test_back_view_mild_kept(self):
        panels = [make_panel(1, shot="背影", intensity=1), make_panel(2, shot="全景")]
        sb, _ = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].shot == "背影"


class TestRhythm:
    def test_six_panels_need_three_emotions(self):
        panels = [make_panel(i, emotion="开心") for i in range(1, 7)]
        with pytest.raises(EmotionRuleError, match="至少 3 种"):
            validate(make_sb(panels), CFG, KNOWN, SOURCES)

    def test_rhythm_soft_mode_warns_instead(self):
        panels = [make_panel(i, emotion="开心") for i in range(1, 7)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES, soft=True)
        assert len(sb.panels) == 6
        assert any("情绪节奏" in w for w in warns)

    def test_calm_ratio_limit(self):
        panels = [make_panel(i, emotion="平静") for i in range(1, 4)]
        with pytest.raises(EmotionRuleError, match="平静"):
            validate(make_sb(panels), CFG, KNOWN, SOURCES)

    def test_calm_consecutive_limit(self):
        emos = ["开心", "平静", "平静", "平静", "生气", "开心"]
        panels = [make_panel(i + 1, emotion=emos[i]) for i in range(6)]
        with pytest.raises(EmotionRuleError, match="连续"):
            validate(make_sb(panels), CFG, KNOWN, SOURCES)

    def test_highlight_must_be_intensity_3(self):
        panels = [make_panel(1, weight="highlight", intensity=2), make_panel(2)]
        with pytest.raises(EmotionRuleError, match="intensity"):
            validate(make_sb(panels), CFG, KNOWN, SOURCES)


class TestCharacters:
    def test_unknown_id_removed(self):
        panels = [make_panel(1, characters=["demo", "ghost"]), make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].characters == ["demo"]
        assert any("未知角色" in w for w in warns)


class TestSource:
    def test_unknown_source_falls_back(self):
        panels = [make_panel(1, source="25:99"), make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert sb.panels[0].source == "all"
        assert any("source" in w for w in warns)

    def test_known_source_kept(self):
        panels = [make_panel(1, source="09:30"), make_panel(2, source="21:00")]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, {"09:30", "21:00"})
        assert [p.source for p in sb.panels] == ["09:30", "21:00"]
        assert not any("source" in w for w in warns)


class TestVisualPrompt:
    def test_chinese_in_visual_prompt_warns(self):
        panels = [make_panel(1, visual_prompt_en="女孩在跑步"), make_panel(2)]
        _, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert any("中文" in w for w in warns)

    def test_emotion_conflict_warns_only(self):
        panels = [make_panel(1, emotion="哭", visual_prompt_en="a girl smiling in the rain"),
                  make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert "smiling" in sb.panels[0].visual_prompt_en  # 第三轮：只 warning 不改文本
        assert any("矛盾" in w for w in warns)

    def test_emotion_conflict_negation_ignored(self):
        panels = [make_panel(1, emotion="哭",
                             visual_prompt_en="a girl in the rain, not smiling"),
                  make_panel(2)]
        _, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert not any("矛盾" in w for w in warns)

    def test_emotion_conflict_word_boundary(self):
        # "nomad" 里的 mad 不是情绪词，词边界不匹配，不误报
        panels = [make_panel(1, emotion="生气", visual_prompt_en="a nomad walks at dusk"),
                  make_panel(2)]
        _, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert not any("矛盾" in w for w in warns)

    def test_matching_emotion_keyword_kept(self):
        panels = [make_panel(1, emotion="哭", visual_prompt_en="a girl crying in the rain"),
                  make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert "crying" in sb.panels[0].visual_prompt_en
        assert not any("矛盾" in w for w in warns)


class TestTextLength:
    def test_narration_truncated_with_ellipsis(self):
        panels = [make_panel(1, narration="长" * 50), make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert len(sb.panels[0].narration) == TEXT_LIMIT
        assert sb.panels[0].narration.endswith("…")
        assert any("旁白" in w for w in warns)

    def test_dialogue_truncated_with_ellipsis(self):
        panels = [make_panel(1, dialogue=[Dialogue(speaker="demo", text="长" * 50)]),
                  make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert len(sb.panels[0].dialogue[0].text) == TEXT_LIMIT
        assert sb.panels[0].dialogue[0].text.endswith("…")
        assert any("对白" in w for w in warns)

    def test_dialogue_over_two_truncated(self):
        dlg = [Dialogue(speaker="demo", text=str(i)) for i in range(4)]
        panels = [make_panel(1, dialogue=dlg), make_panel(2)]
        sb, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert len(sb.panels[0].dialogue) == 2
        assert any("对白" in w for w in warns)


class TestIds:
    def test_ids_renumbered(self):
        panels = [make_panel(5), make_panel(9), make_panel(20)]
        sb, _ = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert [p.id for p in sb.panels] == [1, 2, 3]

    def test_warning_uses_renumbered_id(self):
        panels = [make_panel(7, characters=["ghost"]), make_panel(8)]
        _, warns = validate(make_sb(panels), CFG, KNOWN, SOURCES)
        assert any("第 1 格" in w for w in warns)


class TestAliases:
    def test_shot_alias_normalized(self):
        assert make_panel(1, shot="中近景").shot == "近景"
        assert make_panel(1, shot="大远景").shot == "远景"
        assert make_panel(1, shot="背面").shot == "背影"

    def test_emotion_alias_normalized(self):
        assert make_panel(1, emotion="沮丧").acting.emotion == "难过"
        assert make_panel(1, emotion="微笑").acting.emotion == "开心"
        assert make_panel(1, emotion="破防").acting.emotion == "崩溃"
        assert make_panel(1, emotion="无奈").acting.emotion == "无语"

    def test_unknown_emotion_becomes_calm_with_note(self):
        notes: list[str] = []
        Storyboard.model_validate(
            make_sb([make_panel(1), make_panel(2)]).model_dump()
            | {"panels": [dict(make_panel(1).model_dump(), acting={"emotion": "云淡风轻"}),
                          make_panel(2).model_dump()]},
            context={"notes": notes})
        assert any("未知情绪" in n for n in notes)

    def test_unknown_shot_still_rejected(self):
        with pytest.raises(Exception):
            make_panel(1, shot="航拍")
