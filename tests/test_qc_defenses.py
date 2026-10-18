"""第四轮防线：图片存在性检查（A2）、质检输入核对 / qc_error（A3）、
特征编号匹配（A4）、表情 n/a（A5）、预算两池（A10）。"""

import pytest
from pathlib import Path

from visdiary import budget
from visdiary.config import Config, BudgetConfig
from visdiary.providers.mock import MockImage, MockVision
from visdiary.qc.checker import (QCInputError, check_panel, expression_fail_reason,
                                 identity_passes)
from visdiary.schemas import (Acting, Character, ExpressionCheck, Feature, Panel,
                              QCCheck, QCResult)


def make_char() -> Character:
    return Character(id="demo", name="n", source_image="s.png", style_detected="x",
                     style_confidence=0.9,
                     identity=[Feature(name="发色", value="银白", critical=True),
                               Feature(name="痣", value="泪痣", critical=False)])


def make_panel(**kw):
    base = dict(id=1, shot="近景", scene="s", characters=["demo"],
                acting=Acting(emotion="开心", intensity=2),
                action="a", visual_prompt_en="s", source="all")
    base.update(kw)
    return Panel(**base)


class TestA2ImageExists:
    def test_missing_image_raises_before_any_work(self, tmp_path):
        ghost = tmp_path / "ghost.png"
        with pytest.raises(FileNotFoundError, match="第 1 张"):
            MockVision().analyze_json("p", [ghost], QCResult)
        with pytest.raises(FileNotFoundError, match="参考图不存在"):
            MockImage().generate("p", [("身份图", ghost)], "1:1")
        # 没有 PNG 产出，说明检查发生在画图之前
        assert not list(tmp_path.glob("*.png"))

    def test_missing_image_no_codex_call(self):
        """codex provider：缺图直接抛，不会真的调用 codex exec。"""
        from visdiary.providers.codex import _check_images, _chat_json
        with pytest.raises(FileNotFoundError):
            _check_images([Path("/nonexistent/x.png")], "chat_json")
        # _chat_json 在 subprocess 之前就会抛
        with pytest.raises(FileNotFoundError):
            _chat_json("s", "u", QCResult, "", images=[Path("/nonexistent/x.png")])


class TestA3InputCheck:
    class OneImageVision(MockVision):
        """模拟模型只收到 1 张图（比如第二张被静默丢掉）。"""
        calls = 0

        def analyze_json(self, prompt, images, schema, context=None):
            TestA3InputCheck.OneImageVision.calls += 1
            r = super().analyze_json(prompt, images, schema, context)
            r.images_received = 1  # 声称只收到 1 张
            return r

    def test_images_received_mismatch_raises(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"x")
        ref = tmp_path / "b.png"
        ref.write_bytes(b"y")
        char = make_char()
        with pytest.raises(QCInputError, match="收到"):
            check_panel(_cfg(), self.OneImageVision(), char, make_panel(),
                        img, ref, True)
        assert self.OneImageVision.calls == 2  # 重试了 1 次

    class UnjudgeableVision(MockVision):
        def analyze_json(self, prompt, images, schema, context=None):
            r = super().analyze_json(prompt, images, schema, context)
            r.judgeable = False
            r.unjudgeable_reason = "图加载失败"
            return r

    def test_unjudgeable_raises(self, tmp_path):
        img = tmp_path / "a.png"; img.write_bytes(b"x")
        ref = tmp_path / "b.png"; ref.write_bytes(b"y")
        with pytest.raises(QCInputError, match="图加载失败"):
            check_panel(_cfg(), self.UnjudgeableVision(), make_char(), make_panel(),
                        img, ref, True)


def _cfg():
    from visdiary.config import load_config
    return load_config()


class TestA4FeatureIds:
    def _result(self, checks):
        return QCResult(images_received=2, judgeable=True, character_visible=True,
                        has_text=False, checks=checks)

    def test_renamed_feature_still_matched(self):
        # 模型改写名字（头顶元素→头顶猫耳），编号对得上就算
        char = make_char()
        r = self._result([QCCheck(name="银色头发!!!", feature_id=1, result="pass")])
        ok, problems, _notes = identity_passes(r, char, True)
        assert ok, problems

    def test_missing_critical_counts_as_fail(self):
        char = make_char()
        r = self._result([])  # 漏了所有特征
        ok, problems, _notes = identity_passes(r, char, True)
        assert not ok
        assert any("漏了" in p for p in problems)

    def test_duplicate_feature_id_fails(self):
        char = make_char()
        r = self._result([QCCheck(name="a", feature_id=1, result="pass"),
                          QCCheck(name="b", feature_id=1, result="pass")])
        ok, problems, _notes = identity_passes(r, char, True)
        assert not ok and any("重复" in p for p in problems)

    def test_out_of_range_id_ignored_with_problem(self):
        char = make_char()
        r = self._result([QCCheck(name="a", feature_id=1, result="pass"),
                          QCCheck(name="z", feature_id=99, result="fail")])
        ok, problems, _notes = identity_passes(r, char, True)
        assert ok  # 越界的 fail 不算数
        assert any("越界" in n for n in _notes)  # 越界只警告，不算失败


class TestA5NotApplicable:
    def test_invisible_character_is_na(self):
        r = QCResult(images_received=2, judgeable=True, character_visible=False,
                     has_text=False, checks=[],
                     expression=ExpressionCheck(detected_emotion="开心",
                                                detected_top2=[], detected_intensity=2))
        assert expression_fail_reason(r, make_panel(), True) == "n/a"

    def test_unjudgeable_is_na(self):
        r = QCResult(images_received=2, judgeable=False, character_visible=True,
                     has_text=False, checks=[], expression=None)
        assert expression_fail_reason(r, make_panel(), True) == "n/a"


class TestA10BudgetPools:
    @pytest.fixture
    def cfg(self, tmp_path):
        return Config(root=tmp_path, budget=BudgetConfig(monthly_images=300))

    def test_migration_from_flat(self, cfg):
        p = cfg.path("usage"); p.mkdir(parents=True)
        (p / "2026-10.json").write_text('{"images": 334, "llm_calls": 212}', encoding="utf-8")
        assert budget.usage(cfg, "prod")["images"] == 334  # 旧数据归 prod

    def test_dev_pool_unlimited(self, cfg):
        cfg.env = "dev"
        budget.record(cfg, images=99999)
        n, notice = budget.clamp_panel_count(cfg, 12)  # dev 不熔断
        assert n == 12 and notice is None

    def test_prod_pool_blocked(self, cfg):
        cfg.env = "prod"
        budget.record(cfg, images=99999, pool="prod")
        with pytest.raises(budget.BudgetError):
            budget.clamp_panel_count(cfg, 4)

    def test_dev_record_does_not_burn_prod(self, cfg):
        cfg.env = "dev"
        budget.record(cfg, images=10)
        assert budget.usage(cfg, "prod")["images"] == 0
        assert budget.usage(cfg, "dev")["images"] == 10
