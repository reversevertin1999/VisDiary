"""M14 人设（Persona）验收：空人设向后兼容、缩放、上限/禁用、override 优先级。"""

from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.render.prompt_builder import build_prompt
from visdiary.schemas import Acting, EmotionOverride, Panel, Persona
from visdiary.storyboard.rules import validate
from visdiary.config import StoryboardConfig
from visdiary.schemas import Storyboard

CFG = load_config()
CHAR = load_character(CFG, "demo")
RULECFG = StoryboardConfig(min_panels=2, max_panels=12)
EMOS = ["开心", "难过", "期待", "生气", "感动", "惊讶", "坚定", "大笑"]


def make_panel(i=1, **kw):
    emo = kw.pop("emotion", EMOS[i % len(EMOS)])
    intensity = kw.pop("intensity", 2)
    base = dict(id=i, shot="中景", scene="s", characters=["demo"],
                acting=Acting(emotion=emo, intensity=intensity),
                action="a", visual_prompt_en="s", source="all")
    base.update(kw)
    return Panel(**base)


def test_empty_persona_unchanged():
    p = make_panel(emotion="开心", intensity=3)
    a = build_prompt(CFG, CHAR, p, "anime_cel", {})
    char2 = CHAR.model_copy(deep=True)
    char2.persona = Persona()
    b = build_prompt(CFG, char2, p, "anime_cel", {})
    assert a == b  # 空人设 = 没有人设


def test_expressiveness_scales_down():
    persona = Persona(expressiveness=0.6)
    panels = [make_panel(1, intensity=3), make_panel(2, intensity=3)]
    sb, warns = validate(Storyboard(date="d", title="t", mood="m", panels=panels),
                         RULECFG, ["demo"], {"all"}, persona=persona, soft=True)
    assert all(p.acting.intensity == 2 for p in sb.panels)  # round(3*0.6)=2
    assert any("人设生效" in w for w in warns)


def test_max_intensity_and_banned():
    persona = Persona(max_intensity={"大笑": 2}, banned=["崩溃", "chibi"])
    panels = [make_panel(1, emotion="大笑", intensity=3),
              make_panel(2, emotion="崩溃", intensity=3)]
    panels[1].acting.chibi = True
    sb, warns = validate(Storyboard(date="d", title="t", mood="m", panels=panels),
                         RULECFG, ["demo"], {"all"}, persona=persona, soft=True)
    assert sb.panels[0].acting.intensity == 2            # 上限生效
    assert sb.panels[0].acting.chibi is False            # chibi 禁用生效
    assert sb.panels[1].acting.emotion != "崩溃"          # banned 情绪被归位
    assert any("人设生效" in w for w in warns)


def test_override_priority_in_prompt():
    char2 = CHAR.model_copy(deep=True)
    char2.persona = Persona(overrides={"生气": EmotionOverride(
        face={2: "puffed cheeks, pouting hard, NOT yelling"}, ref_view=None)})
    panel = make_panel(emotion="生气", intensity=2)
    p = build_prompt(CFG, char2, panel, "anime_cel", {})
    assert "puffed cheeks, pouting hard, NOT yelling" in p
    # 模板默认（clear frown...）被 override 顶掉
    assert "clear frown" not in p
    # 面板自带的 face_en 优先级最高
    panel.acting.face_en = "user custom face"
    p2 = build_prompt(CFG, char2, panel, "anime_cel", {})
    assert "user custom face" in p2
