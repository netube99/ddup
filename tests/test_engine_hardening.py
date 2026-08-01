"""引擎加固项测试：max_positions 硬上限、非法价格防护、涨跌停舍入、
rebalance 零碎股清仓、run 异常状态落库。"""

import logging
import sqlite3

import pandas as pd
import pytest

from btcore.costs import calc_trade_costs
from btcore.database import init_backtest_db
from btcore.engine import Engine
from btcore.limits import get_limit_prices
from btcore.match.manual import manual_buy, manual_sell, rebalance_to_targets
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage
from tests.conftest import MockDataBackend, make_account, make_bar, make_holding

NAN = float("nan")


def _account(cash=1_000_000.0, holdings=None):
    return make_account(cash=cash, holdings=holdings, slippage_ticks=0)


def _holding(symbol="000001.SZ", shares=1000, price=10.0):
    return make_holding(symbol=symbol, shares=shares, entry_price=price)


# ── max_positions 软告警（不再硬截断）──


def test_manual_buy_warns_on_max_positions():
    account = _account()
    bars = {s: make_bar() for s in ("000001.SZ", "000002.SZ", "000003.SZ")}
    trades = manual_buy(account, bars, list(bars), 2,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert len(trades) == 3  # max_positions 不再硬截断
    assert len(account.holdings) == 3


def test_manual_buy_no_longer_blocked_by_max_positions():
    account = _account(holdings={"000001.SZ": _holding()})
    trades = manual_buy(account, {"000002.SZ": make_bar()}, ["000002.SZ"], 2,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert len(trades) == 1  # max_positions 不阻止新买
    assert "000002.SZ" in account.holdings


# ── 非法价格防护 ──


def test_sell_skips_nan_open():
    holding = _holding()
    account = _account(cash=0.0, holdings={"000001.SZ": holding})
    bar = make_bar()
    bar["open"] = NAN
    trades = manual_sell(account, {"000001.SZ": bar}, ["000001.SZ"],
                         get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert account.cash == 0.0
    assert account.holdings["000001.SZ"].shares == 1000


def test_buy_skips_nan_open():
    account = _account()
    bar = make_bar()
    bar["open"] = NAN
    trades = manual_buy(account, {"000001.SZ": bar}, ["000001.SZ"], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert account.cash == 1_000_000.0


def test_rebalance_skips_nan_open():
    account = _account(holdings={"000001.SZ": _holding()})
    bar = make_bar()
    bar["open"] = NAN
    trades = rebalance_to_targets(account, {"000001.SZ": bar},
                                  {"000001.SZ": 0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)
    assert trades == []
    assert account.holdings["000001.SZ"].shares == 1000


def test_settle_ignores_nan_close():
    class _S:
        config = {}

    engine = Engine(_S(), None, initial_capital=1_000_000, db_path=":memory:")
    engine.account.holdings["000001.SZ"] = _holding()
    conn = init_backtest_db(":memory:")
    try:
        engine._settle("20240603", {"000001.SZ": {"close": NAN}}, [], [], conn)
    finally:
        conn.close()
    holding = engine.account.holdings["000001.SZ"]
    assert holding.last_price == 10.0
    assert engine.account.total_value == engine.account.cash + 1000 * 10.0


# ── 涨跌停推算: 交易所四舍五入口径 ──


def test_limit_rounding_half_up():
    # 10.05 × 1.1 = 11.055 → 11.06 (二进制浮点 + 银行家舍入会错给 11.05)
    # 10.05 × 0.9 = 9.045  → 9.05
    up, down = get_limit_prices("000001.SZ", {"pre_close": 10.05}, "20240603")
    assert up == 11.06
    assert down == 9.05


# ── rebalance 显式清仓: 零碎股一并卖出 ──


def test_rebalance_full_exit_sells_odd_lot():
    account = _account(cash=0.0,
                       holdings={"000001.SZ": _holding(shares=150)})
    trades = rebalance_to_targets(account, {"000001.SZ": make_bar()},
                                  {"000001.SZ": 0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)
    assert trades[0].shares == 150
    assert "000001.SZ" not in account.holdings


def test_rebalance_partial_reduction_keeps_lot_truncation():
    # 目标市值 500 (50 股): 减仓按整手截断, 卖 100 股留 50
    account = _account(cash=0.0,
                       holdings={"000001.SZ": _holding(shares=150)})
    trades = rebalance_to_targets(account, {"000001.SZ": make_bar()},
                                  {"000001.SZ": 500.0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)
    assert trades[0].shares == 100
    assert account.holdings["000001.SZ"].shares == 50


# ── run 异常: 状态落 failed ──


class _BoomStrategy:
    def __init__(self, config=None):
        self.config = config or {}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def select(self, bars, snapshot, provider):
        raise RuntimeError("boom")

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_run_marks_failed_on_exception(tmp_path):
    db = str(tmp_path / "bt.db")
    provider = DataProvider(MockDataBackend())
    engine = Engine(_BoomStrategy(), provider, initial_capital=1_000_000,
                    db_path=db)
    with pytest.raises(RuntimeError, match="boom"):
        engine.run("20240603", "20240607")

    conn = sqlite3.connect(db)
    try:
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
    finally:
        conn.close()
    assert status == "failed"


# ── select() 协议加固: 名单查重 / 返回类型 / 未知键 / 前视钳制 ──


class _DuckStrategy:
    """最小鸭子策略：select/on_tick 返回可注入结果。"""

    config = {}

    def __init__(self, actions=None, tick_result=None):
        self._actions = actions if actions is not None else {"buy": [], "sell": []}
        self._tick_result = tick_result

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def on_start(self, provider, first_date, end_date=None):
        pass

    def on_fills(self, trades, provider):
        pass

    def on_tick(self, bars, snapshot, provider):
        return self._tick_result

    def select(self, bars, account_snapshot, provider):
        return dict(self._actions)

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def _make_engine(strategy):
    provider = DataProvider(MockDataBackend())
    return Engine(strategy, provider, db_path=":memory:")


def test_buy_duplicate_symbols_raise():
    """buy 名单重复 symbol → 决策时点 ValueError（双重扣款 + 持仓覆盖的前置拦截）。"""
    engine = _make_engine(_DuckStrategy(
        actions={"buy": ["000001.SZ", "000001.SZ"], "sell": []}
    ))
    with pytest.raises(ValueError, match="重复 symbol"):
        engine._compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_sell_duplicate_symbols_raise():
    engine = _make_engine(_DuckStrategy(
        actions={"buy": [], "sell": ["000001.SZ", "000001.SZ"]}
    ))
    with pytest.raises(ValueError, match="重复 symbol"):
        engine._compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_select_non_dict_raises():
    class NotDict(_DuckStrategy):
        def select(self, bars, account_snapshot, provider):
            return None

    engine = _make_engine(NotDict())
    with pytest.raises(ValueError, match="必须返回 dict"):
        engine._compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_select_unknown_key_warns(caplog):
    """select 返回未知键（typo 如 buy_condition）→ WARNING 而不是静默失效。"""
    engine = _make_engine(_DuckStrategy(
        actions={"buy": [], "sell": [], "buy_condition": ["000001.SZ"]}
    ))
    with caplog.at_level(logging.WARNING):
        engine._compute_pending("20240603", {"000001.SZ": make_bar()}, [])
    assert "未知键" in caplog.text


def test_on_tick_non_dict_raises():
    engine = _make_engine(_DuckStrategy(tick_result=[]))
    with pytest.raises(ValueError, match="on_tick"):
        engine._compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_condition_missing_price_fails_fast():
    """条件单缺必填键（STOP_LOSS 无 price）→ 决策时点 ValueError，不拖到次日撮合。"""

    class NoPriceCond(_DuckStrategy):
        def calc_conditions(self, symbol, entry_price, bar, holding_days):
            return [{"type": "STOP_LOSS"}]

    engine = _make_engine(NoPriceCond())
    engine.account.holdings["000001.SZ"] = make_holding(
        symbol="000001.SZ", shares=1000
    )
    with pytest.raises(ValueError, match="缺必填键"):
        engine._compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_provider_clamp_blocks_future():
    """set_as_of 后 provider 查询端被钳制；未钳制时同一查询可读到未来。"""
    provider = DataProvider(MockDataBackend())
    idx = pd.MultiIndex.from_tuples(
        [(d, "000001.SZ") for d in ("20240501", "20240603", "20240604", "20240605")],
        names=["trade_date", "symbol"],
    )
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    provider.attach_bars(df)

    unclamped = provider.get_historical_bars(None, "20240610", lookback_days=1000)
    assert {"20240604", "20240605"} <= set(
        unclamped.index.get_level_values("trade_date")
    )

    provider.set_as_of("20240604")
    clamped = provider.get_historical_bars(None, "20240610", lookback_days=1000)
    dates = set(clamped.index.get_level_values("trade_date"))
    assert dates == {"20240501", "20240603"}


def test_on_start_runs_with_clamped_asof():
    """run() 在 on_start 前已钳制 provider（前视窗口闭合）。"""
    seen = {}

    def check(provider):
        seen["as_of"] = provider._as_of_date

    strategy = _DuckStrategy()
    strategy.on_start = lambda provider, first_date, end_date=None: check(provider)
    engine = _make_engine(strategy)
    engine.run("20240603", "20240607")
    assert seen["as_of"] is not None
    assert seen["as_of"] <= "20240603"


def test_run_idempotent_second_run_resets_account():
    """同一实例二次 run() 从头重置账户，两次结果完全一致。"""
    strategy = _DuckStrategy(actions={"buy": ["000001.SZ"], "sell": []})
    engine = _make_engine(strategy)
    engine.run("20240603", "20240607")
    assert engine.account.cash < engine.initial_capital  # 发生了买入
    first = (engine.account.cash, len(engine.account.holdings))
    engine.run("20240603", "20240607")
    second = (engine.account.cash, len(engine.account.holdings))
    assert first == second
