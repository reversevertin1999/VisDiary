"""第二轮表情系统的专项测试。"""

import io
import json

import pytest
from PIL import Image

from visdiary.character.builder import slice_expression_grid
from visdiary.character.prompts import BUST_VIEWS
from visdiary.character.store import load_character
from visdiary.config import load_config
from visdiary.qc.checker import expression_fail_reason
from visdiary.render import generator
from visdiary.render.cache import cache_key
from visdiary.schemas import (EMOTIONS, Acting, ExpressionCheck, Panel, QCCheck,
                              QCResult, Storyboard)
from visdiary.styles import load_emotions

CFG = load_config()
CHAR = load_character(CFG, "demo")


def make_panel(**kw):
    emo = kw.pop("emotion", "开心")
    intensity = kw.pop("intensity", 2)
    base = dict(id=1, shot="近景", scene="s", characters=["demo"],
                acting=Acting(emotion=emo, intensity=intensity),
                action="a", visual_prompt_en="s", source="all")
    base.update(kw)
    return Panel(**base)


class TestEmotionsYaml:
    def test_all_21_emotions_covered(self):
        emotions, manpu_en = load_emotions(CFG)
        assert set(emotions) == set(EMOTIONS)
        assert len(emotions) == 21

    def test_each_emotion_complete(self):
        emotions, _ = load_emotions(CFG)
        bust_ids = {v for v, _d in BUST_VIEWS}
        for name, p in emotions.items():
            assert p.ref_view is None or p.ref_view in bust_ids, name
            assert set(p.levels) == {1, 2, 3}, name
            for lv in p.levels.values():
                assert lv.face and lv.body and lv.body_close, name
            assert p.en and p.desc and p.shots, name

    def test_manpu_en_complete(self):
        from visdiary.schemas import MANPUS
        _, manpu_en = load_emotions(CFG)
        assert set(manpu_en) == set(MANPUS)


class TestBackwardCompat:
    def test_old_expression_becomes_acting(self):
        old = {"id": 1, "shot": "中景", "scene": "s", "characters": ["demo"],
               "expression": "开心", "action": "a", "visual_prompt_en": "s",
               "source": "all"}
        p = Panel(**old)
        assert p.acting.emotion == "开心"
        assert p.acting.intensity == 2
        assert p.acting.inner_voice == ""

    def test_old_storyboard_json_readable(self):
        old_sb = json.dumps({
            "date": "2026-10-09", "title": "旧", "mood": "晴",
            "panels": [{"id": 1, "shot": "近景", "scene": "s", "characters": ["demo"],
                        "expression": "疲惫", "action": "a", "visual_prompt_en": "s",
                        "source": "all"}]})
        sb = Storyboard.model_validate_json(old_sb)
        assert sb.panels[0].acting.emotion == "疲惫"


class TestCacheAttempt:
    def test_attempt_changes_key(self, tmp_path):
        f = tmp_path / "r.png"
        f.write_bytes(b"x")
        assert cache_key("p", [f], "m", "m0", "1:1", None, 0) != \
               cache_key("p", [f], "m", "m0", "1:1", None, 1)

    def test_redraw_skips_cache(self, project_tmp):
        import uuid
        from visdiary.providers.mock import MockImage
        panel = make_panel(visual_prompt_en=f"unique scene {uuid.uuid4()}")  # 每次测试都换新键
        outdir = project_tmp / "gen"
        outdir.mkdir(parents=True, exist_ok=True)
        m1 = generator.generate_panel(CFG, MockImage(), CHAR, panel, "1:1",
                                      "anime_cel", {}, outdir)
        assert not m1["cache_hit"]
        m2 = generator.generate_panel(CFG, MockImage(), CHAR, panel, "1:1",
                                      "anime_cel", {}, outdir)
        assert m2["cache_hit"]  # 同输入第二次命中
        m3 = generator.generate_panel(CFG, MockImage(), CHAR, panel, "1:1",
                                      "anime_cel", {}, outdir, attempt=1)
        assert not m3["cache_hit"]  # 重画不读缓存


class TestSlice:
    def test_grid_sliced_into_12_tiles(self):
        img = Image.new("RGB", (1200, 1600), (255, 0, 0))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        tiles = slice_expression_grid(buf.getvalue())
        assert len(tiles) == 12
        for t in tiles:
            assert Image.open(io.BytesIO(t)).size == (400, 400)


class TestExpressionPasses:
    def _result(self, emo, intensity, face="large", top2=None):
        return QCResult(character_visible=True, has_text=False, checks=[],
                        expression=ExpressionCheck(detected_emotion=emo,
                                                   detected_top2=top2 or [],
                                                   detected_intensity=intensity,
                                                   face_size=face))

    def test_same_group_ok(self):
        p = make_panel(emotion="哭", intensity=2)
        assert expression_fail_reason(self._result("大哭", 2), p, True) is None

    def test_top2_not_used_in_pass_rule(self):
        # A7：top-2 命中只是 bench 的辅助指标，质检判定只看 top-1
        p = make_panel(emotion="委屈", intensity=2)
        assert expression_fail_reason(self._result("开心", 2, top2=["难过"]), p, True) == "category"

    def test_category_mismatch_fails(self):
        p = make_panel(emotion="哭")
        assert expression_fail_reason(self._result("开心", 2), p, True) == "category"

    def test_intensity_weak(self):
        p = make_panel(emotion="生气", intensity=3)
        assert expression_fail_reason(self._result("生气", 2), p, True) is None   # 差 1 级容忍
        assert expression_fail_reason(self._result("生气", 1), p, True) == "weak"

    def test_intensity_overacted(self):
        p = make_panel(emotion="平静", intensity=1)
        assert expression_fail_reason(self._result("平静", 2), p, True) is None  # 目标1级容忍2级
        assert expression_fail_reason(self._result("平静", 3), p, True) == "strong"

    def test_face_size_only_close_shots(self):
        p = make_panel(emotion="开心", shot="近景")
        assert expression_fail_reason(self._result("开心", 2, face="small"), p, True) == "face"
        p_far = make_panel(emotion="开心", shot="全景")  # 全景不再误判
        assert expression_fail_reason(self._result("开心", 2, face="small"), p_far, True) is None

    def test_no_expression_info_returns_none(self):
        p = make_panel()
        # P1-1：主角可见但判官没给表情结论 → n/a（不计入命中率分母）
        assert expression_fail_reason(QCResult(images_received=2, judgeable=True,
                                               character_visible=True, has_text=False,
                                               checks=[]), p, True) == "n/a"
        # 纯场景格：不适用
        assert expression_fail_reason(self._result("开心", 2), p, False) is None


class TestFailedPlaceholder:
    def test_failed_panel_becomes_placeholder(self, project_tmp):
        from visdiary.layout.planner import plan_layout

        class BrokenImage:
            name = "broken"
            model = "broken-0"

            def generate(self, *a, **kw):
                raise RuntimeError("api down")

        cfg = CFG.model_copy(deep=True)
        cfg.generation.api_retries = 1
        sb = Storyboard(date="2026-10-08", title="t", mood="m", panels=[make_panel()])
        layout = plan_layout(sb, CFG.layout)
        outdir = project_tmp / "fail"
        outdir.mkdir(parents=True, exist_ok=True)
        metas = generator.generate_all(cfg, BrokenImage(), CHAR, sb, layout,
                                       "anime_cel", outdir)
        assert metas[1]["flagged"] is True
        assert "api down" in metas[1]["error"]
        assert (outdir / "panels" / "01.png").is_file()  # 占位图在，整页还能排
