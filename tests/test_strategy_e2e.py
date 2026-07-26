"""策略层端到端测试：YAML 示例策略 + MockDataBackend 走完整 Engine.run。

全链路写法参考 tests/test_foreign_backend.py。
"""

from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from tests.conftest import MockDataBackend

EXAMPLE_YAML = "strategies/examples/topk_momentum.yaml"


def test_example_strategy_full_run():
    strategy = load_strategy(EXAMPLE_YAML)
    provider = DataProvider(MockDataBackend())
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    result = engine.run("20240603", "20240614")

    account_daily = result["account_daily"]
    trade_log = result["trade_log"]
    stats = result["statistics"]

    assert len(account_daily) > 0
    assert len(trade_log) > 0
    assert isinstance(stats, dict)
    # 现金始终非负
    assert (account_daily["cash"] >= 0).all()
    # 期末总资产 = 现金 + 持仓市值
    final = account_daily.iloc[-1]
    holdings_value = sum(
        h.shares * h.last_price for h in engine.account.holdings.values()
    )
    assert abs(final["total_value"] - (final["cash"] + holdings_value)) < 1e-6
    # 持仓数不超过 max_positions
    assert len(engine.account.holdings) <= engine.max_positions


def test_benchmark_default_present():
    """默认 benchmark=000300.SH，MockDataBackend 有基准数据 → 有对比指标。"""
    strategy = load_strategy(EXAMPLE_YAML)
    provider = DataProvider(MockDataBackend())
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    result = engine.run("20240603", "20240607")

    assert result["statistics"]["benchmark_compare"]


def test_benchmark_disabled_by_empty_config():
    """config benchmark 置空字符串 → 不取基准，statistics 无基准指标。"""
    strategy = load_strategy(EXAMPLE_YAML)
    strategy.config["benchmark"] = ""
    provider = DataProvider(MockDataBackend())
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    result = engine.run("20240603", "20240607")

    assert result["statistics"]["benchmark_compare"] == {}
