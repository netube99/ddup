"""report/cross_validate 指标键清单与 stats 输出的漂移锁。

research/report.py 的五张 SPEC（+ _COMPARE_SPEC dotted 键）手工对照
calculate_statistics 的输出键，历史上靠人肉同步。本测试用一次真实
fixture 回测（MockDataBackend + 示例策略）的 statistics dict 断言所有
SPEC 键存在；stats 删键/改键或 report 加键未落地时立即失败并列出缺失清单。
"""

import pytest

from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from research.report import (
    _COMPARE_SPEC,
    _COMPLEXITY_SPEC,
    _CORE_SPEC,
    _COST_SPEC,
    _FRICTION_SPEC,
    _ROUND_TRIP_SPEC,
)
from tests.conftest import MockDataBackend

EXAMPLE_YAML = "strategies/examples/rolling_ranker/config.yaml"


@pytest.fixture(scope="module")
def statistics() -> dict:
    strategy = load_strategy(EXAMPLE_YAML)
    engine = Engine(
        strategy, DataProvider(MockDataBackend()), initial_capital=1_000_000
    )
    result = engine.run("20240603", "20240614")
    assert len(result["trade_log"]) > 0  # 保证嵌套块非空，键断言有意义
    return result["statistics"]


def _dig_missing(statistics: dict, dotted: str) -> bool:
    """dotted 路径任一段缺失返回 True。"""
    cur = statistics
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return True
        cur = cur[part]
    return False


def _missing_keys(statistics: dict, spec: list, prefix: str = "") -> list[str]:
    return [
        prefix + key
        for _, key, _ in spec
        if _dig_missing(statistics, prefix + key)
    ]


def test_core_spec_keys(statistics):
    missing = _missing_keys(statistics, _CORE_SPEC)
    assert not missing, f"_CORE_SPEC 缺失键: {missing}"


def test_friction_spec_keys(statistics):
    missing = _missing_keys(statistics, _FRICTION_SPEC, "trading_friction.")
    assert not missing, f"_FRICTION_SPEC 缺失键: {missing}"


def test_cost_spec_keys(statistics):
    missing = _missing_keys(statistics, _COST_SPEC, "cost_breakdown.")
    assert not missing, f"_COST_SPEC 缺失键: {missing}"


def test_complexity_spec_keys(statistics):
    missing = _missing_keys(statistics, _COMPLEXITY_SPEC, "management_complexity.")
    assert not missing, f"_COMPLEXITY_SPEC 缺失键: {missing}"


def test_round_trip_spec_keys(statistics):
    missing = _missing_keys(statistics, _ROUND_TRIP_SPEC, "round_trip.summary.")
    assert not missing, f"_ROUND_TRIP_SPEC 缺失键: {missing}"


def test_compare_spec_keys(statistics):
    missing = _missing_keys(statistics, _COMPARE_SPEC)
    assert not missing, f"_COMPARE_SPEC 缺失键: {missing}"
