from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.render.prompt_builder import build_prompt
from visdiary.render.refs import select_refs
from visdiary.schemas import Acting, Panel

CFG = load_config()
CHAR = load_character(CFG, "demo")
STYLE = "anime_cel"


def make_panel(**kw):
    emo = kw.pop("emotion", "平静")
    intensity = kw.pop("intensity", 2)
    base = dict(id=1, shot="中景", scene="场景", characters=["demo"],
                acting=Acting(emotion=emo, intensity=intensity),
                action="动作", visual_prompt_en="a scene", source="all")
    base.update(kw)
    return Panel(**base)


def ref_paths(refs):
    return [p for _v, p in refs]


class TestPromptBuilder:
    def test_shot_and_acting_block(self):
        p = build_prompt(CFG, CHAR, make_panel(shot="背影", emotion="哭"), STYLE, {})
        assert "back view, seen from behind" in p
        assert "ACTING (most important)" in p
        assert "crying with tears" in p
        # 表演块在身份块前面
        assert p.index("ACTING") < p.index("CHARACTER IDENTITY")

    def test_intensity_word(self):
        p = build_prompt(CFG, CHAR, make_panel(emotion="大笑", intensity=3), STYLE, {})
        assert "exaggerated manga-style, over-the-top" in p

    def test_manpu_included(self):
        panel = make_panel(emotion="生气")
        panel.acting.manpu = ["青筋"]
        p = build_prompt(CFG, CHAR, panel, STYLE, {})
        assert "cross-shaped anger vein mark" in p

    def test_dialogue_adds_space_hint(self):
        from visdiary.schemas import Dialogue
        panel = make_panel(dialogue=[Dialogue(speaker="demo", text="早")])
        assert "leave some empty space" in build_prompt(CFG, CHAR, panel, STYLE, {})

    def test_no_text_banned(self):
        p = build_prompt(CFG, CHAR, make_panel(), STYLE, {})
        assert "no text" in p and "no speech bubbles" in p

    def test_ref_hint_not_copy_expression(self):
        p = build_prompt(CFG, CHAR, make_panel(), STYLE, {},
                         used_views={"front_full", "bust_neutral"})
        assert "Do NOT copy" in p or "must follow ACTING" in p

    def test_extras_used_when_name_mentioned(self):
        panel = make_panel(scene="和室友吃火锅")
        p = build_prompt(CFG, CHAR, panel, STYLE, {"室友": "the roommate, glasses"})
        assert "室友: the roommate, glasses." in p

    def test_extras_matched_by_dialogue_speaker(self):
        from visdiary.schemas import Dialogue
        panel = make_panel(dialogue=[Dialogue(speaker="室友", text="走吗")])
        p = build_prompt(CFG, CHAR, panel, STYLE, {"室友": "the roommate, glasses"})
        assert "室友: the roommate, glasses." in p

    def test_fix_note_appended(self):
        p = build_prompt(CFG, CHAR, make_panel(), STYLE, {}, fix_note="hair must be silver")
        assert p.endswith("IMPORTANT: hair must be silver")


class TestRefs:
    def test_mid_shot_identity_front_full(self):
        refs = select_refs(CFG, CHAR, make_panel(), STYLE)
        assert ref_paths(refs)[0].name == "front_full.png"

    def test_close_shot_identity_bust_neutral(self):
        refs = select_refs(CFG, CHAR, make_panel(shot="近景"), STYLE)
        assert ref_paths(refs)[0].name == "bust_neutral.png"

    def test_expression_bust_added(self):
        refs = select_refs(CFG, CHAR, make_panel(emotion="开心"), STYLE)
        assert any(p.name == "bust_smile.png" for p in ref_paths(refs))

    def test_intensity3_no_neutral_face(self):
        refs = select_refs(CFG, CHAR, make_panel(shot="近景", intensity=3,
                                                 emotion="生气"), STYLE)
        names = [p.name for p in ref_paths(refs)]
        assert "front_full.png" in names
        assert "bust_neutral.png" not in names  # 不用中性脸中和夸张表情
        assert len(refs) <= 2

    def test_back_view_no_expression_ref(self):
        refs = select_refs(CFG, CHAR, make_panel(shot="背影", emotion="哭"), STYLE)
        names = [p.name for p in ref_paths(refs)]
        assert "back_full.png" in names and "front_full.png" in names
        assert not any(n.startswith("bust_") for n in names)  # 背影不给表情图

    def test_max_three_refs(self):
        refs = select_refs(CFG, CHAR, make_panel(shot="近景", emotion="哭"), STYLE)
        assert len(refs) <= 3

    def test_no_protagonist_no_refs(self):
        refs = select_refs(CFG, CHAR, make_panel(characters=[]), STYLE)
        assert refs == []
