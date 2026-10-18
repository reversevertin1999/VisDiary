"""M19 多人物单元测试：一格最多 2 角色、双人镜头、双角色质检分桶、串色。"""

import pytest

from visdiary.config import StoryboardConfig
from visdiary.schemas import Acting, Character, Feature, Panel, QCCheck, QCResult, Storyboard
from visdiary.storyboard.rules import validate

CFG = StoryboardConfig(min_panels=2, max_panels=12)


def make_panel(i=1, characters=("demo",), shot="中景", **kw):
    base = dict(id=i, shot=shot, scene="s", characters=list(characters),
                acting=Acting(emotion="开心", intensity=1 if len(characters) > 1 else 2),
                action="a", visual_prompt_en="s", source="all")
    base.update(kw)
    return Panel(**base)


def make_sb(panels):
    return Storyboard(date="2026-10-08", title="t", mood="m", panels=panels)


CHAR = Character(id="demo", name="n", source_image="s", style_detected="x",
                 style_confidence=.9, identity=[Feature(name="发色", value="银白")])
OTHER = Character(id="roommate", name="小林", source_image="s", style_detected="x",
                  style_confidence=.9, identity=[Feature(name="发色", value="黑色")])


class TestRules:
    def test_max_two_cast_per_panel(self):
        ps = [make_panel(1, characters=("demo", "roommate", "third")),
              make_panel(2)]
        sb, warns = validate(make_sb(ps), CFG, ["demo", "roommate", "third"], {"all"},
                             soft=True)
        assert sb.panels[0].characters == ["demo", "roommate"]
        assert any("超过 2 个" in w for w in warns)

    def test_duo_closeup_becomes_medium(self):
        ps = [make_panel(1, characters=("demo", "roommate"), shot="特写"),
              make_panel(2)]
        sb, warns = validate(make_sb(ps), CFG, ["demo", "roommate"], {"all"},
                             soft=True)
        assert sb.panels[0].shot == "中景"
        assert any("双人" in w for w in warns)


class TestDuoQC:
    def _result(self, checks, cross=None):
        return QCResult(images_received=3, judgeable=True, character_visible=True,
                        has_text=False, checks=checks, cross_contamination=cross)

    def test_both_characters_checked(self):
        from visdiary.qc.checker import identity_passes
        r = self._result([
            QCCheck(feature_id=1, result="pass", character_id="demo"),
            QCCheck(feature_id=101, result="pass", character_id="roommate"),
        ])
        ok, problems, _ = identity_passes(r, CHAR, True)
        assert ok, problems

    def test_side_character_fail_fails(self, _isolated_project):
        import json
        d = _isolated_project / "characters" / "roommate"
        d.mkdir(parents=True, exist_ok=True)
        (d / "character.json").write_text(json.dumps({
            "id": "roommate", "name": "小林", "source_image": "s.png",
            "identity": [{"name": "发色", "value": "黑色", "critical": True}],
            "style_detected": "anime_cel", "style_confidence": 0.9}), encoding="utf-8")
        from visdiary.config import load_config
        from visdiary.qc.checker import identity_passes
        panel = make_panel(1, characters=("demo", "roommate"))
        r = self._result([
            QCCheck(feature_id=1, result="pass", character_id="demo"),
            QCCheck(feature_id=101, result="fail", character_id="roommate"),
        ])
        ok, problems, _ = identity_passes(r, CHAR, True, cfg=load_config(), panel=panel)
        assert not ok and any("roommate" in p for p in problems)

    def test_cross_contamination_fails(self):
        from visdiary.qc.checker import identity_passes
        r = self._result([
            QCCheck(feature_id=1, result="pass", character_id="demo"),
        ], cross=True)
        ok, problems, _ = identity_passes(r, CHAR, True)
        assert not ok and any("互串" in p for p in problems)

    def test_missing_side_character_counts(self):
        from visdiary.qc.checker import identity_passes
        r = self._result([QCCheck(feature_id=1, result="pass")])  # 配角没查
        ok, problems, _ = identity_passes(r, CHAR, True)
        # 单人调用（无 cfg/panel）不查配角：通过
        assert ok

    def test_missing_side_with_ctx(self, _isolated_project, monkeypatch):
        # 临时项目里造 roommate 角色，传 cfg/panel 让配角也被要求
        import json
        root = _isolated_project
        d = root / "characters" / "roommate"
        d.mkdir(parents=True)
        (d / "character.json").write_text(json.dumps({
            "id": "roommate", "name": "小林", "source_image": "s.png",
            "identity": [{"name": "发色", "value": "黑色", "critical": True}],
            "style_detected": "anime_cel", "style_confidence": 0.9}), encoding="utf-8")
        from visdiary.config import load_config
        from visdiary.qc.checker import identity_passes
        cfg = load_config()
        panel = make_panel(1, characters=("demo", "roommate"))
        r = self._result([QCCheck(feature_id=1, result="pass")])
        ok, problems, _ = identity_passes(r, CHAR, True, cfg=cfg, panel=panel)
        assert not ok and any("roommate" in p for p in problems)
