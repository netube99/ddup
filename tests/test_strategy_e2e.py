"""策略层端到端测试：YAML 示例策略 + MockDataBackend 走完整 Engine.run。

全链路写法参考 tests/test_foreign_backend.py。
"""

from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from tests.conftest import MockDataBackend

EXAMPLE_YAML = "strategies/examples/rolling_ranker/config.yaml"


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


def test_rebalance_interval_counts_trading_days_not_date_ints():
    """E2E-01 回归：时间门控按交易日计数器，禁止 int(YYYYMMDD) 十进制差。

    用 self_managed_time 构造 interval=5，区间 20240620-20240701（fixture
    预热含前一日 20240619，引擎在 prev_day 预计算首日信号）：

    - 交易日口径：信号日 20240619 → 20240626（跨 3 个日历周、相隔 5 个
      交易日，恰为第 5 个引擎日）——旧日期整数差在 20240624（跨周末）
      就会强插第二次调仓，本测试钉住该行为不再出现。
    """
    strategy = load_strategy("strategies/examples/self_managed_time/config.yaml")
    strategy.config["rebalance_interval"] = 5
    rebalance_days: list[str] = []
    orig_select = strategy.select

    def recording_select(bars, account_snapshot, provider):
        actions = orig_select(bars, account_snapshot, provider)
        # select 返回后计数器为 0 ⇔ 本次进入调仓分支（非调仓日不清零）
        if strategy._days_since_rebalance == 0:
            rebalance_days.append(next(iter(bars.values())).get("trade_date", ""))
        return actions

    strategy.select = recording_select
    provider = DataProvider(MockDataBackend())
    engine = Engine(strategy, provider, initial_capital=1_000_000)
    result = engine.run("20240620", "20240701")

    assert len(result["account_daily"]) == 8
    assert rebalance_days == ["20240619", "20240626"]
