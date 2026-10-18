import pytest

from visdiary import budget
from visdiary.config import BudgetConfig, Config


@pytest.fixture
def cfg(tmp_path):
    return Config(root=tmp_path, budget=BudgetConfig(monthly_images=300,
                                                     expected_retry_ratio=0.3))


def test_plenty_of_budget_keeps_count(cfg):
    n, notice = budget.clamp_panel_count(cfg, 12)
    assert n == 12 and notice is None


def test_budget_clamps_panel_count(cfg):
    budget.record(cfg, images=290)  # 剩 10 张，约够 7 格
    n, notice = budget.clamp_panel_count(cfg, 12)
    assert n == 7
    assert "下调" in notice


def test_budget_exhausted_stops(cfg):
    budget.record(cfg, images=299)  # 剩 1 张，连 2 格都不够
    with pytest.raises(budget.BudgetError):
        budget.clamp_panel_count(cfg, 4)


def test_usage_persists(cfg):
    budget.record(cfg, images=3, llm_calls=2)
    assert budget.usage(cfg, "prod") == {"images": 3, "llm_calls": 2, "vision_calls": 0}
