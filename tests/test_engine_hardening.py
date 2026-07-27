"""引擎加固项测试：max_positions 硬上限、非法价格防护、涨跌停舍入、
rebalance 零碎股清仓、run 异常状态落库。"""

import sqlite3

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


# ── max_positions 硬上限 ──


def test_manual_buy_respects_max_positions():
    account = _account()
    bars = {s: make_bar() for s in ("000001.SZ", "000002.SZ", "000003.SZ")}
    trades = manual_buy(account, bars, list(bars), 2,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert len(trades) == 2
    assert len(account.holdings) == 2


def test_manual_buy_cap_counts_existing_holdings():
    account = _account(holdings={"000001.SZ": _holding()})
    trades = manual_buy(account, {"000002.SZ": make_bar()}, ["000002.SZ"], 1,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert "000002.SZ" not in account.holdings


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
