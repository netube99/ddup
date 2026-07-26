"""target_value 目标仓位测试：rebalance_to_targets 单测 + Engine 端到端。"""

import math

import pytest

from btcore.costs import calc_trade_costs
from btcore.engine import Engine
from btcore.limits import get_limit_prices
from btcore.match.manual import rebalance_to_targets
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage
from tests.conftest import MockDataBackend, make_account, make_bar, make_holding


def _account(cash=100_000.0, holdings=None):
    return make_account(cash=cash, holdings=holdings, slippage_ticks=0)


def test_partial_sell_shares_and_cash():
    holding = make_holding()
    account = _account(cash=0.0, holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(open=10.0)}

    trades = rebalance_to_targets(account, bars, {"000001.SZ": 5000.0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)

    assert len(trades) == 1
    trade = trades[0]
    assert trade.side == "SELL"
    assert trade.trigger == "TARGET"
    assert trade.shares == 500
    # 持仓股数与成本按比例缩减；现金 = 卖出净额（INV1 式恒等）
    assert account.holdings["000001.SZ"].shares == 500
    assert account.holdings["000001.SZ"].cost == pytest.approx(5000.0)
    assert account.cash == pytest.approx(trade.net_amount)


def test_full_clear_on_zero_target():
    holding = make_holding()
    account = _account(cash=0.0, holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(open=10.0)}

    trades = rebalance_to_targets(account, bars, {"000001.SZ": 0.0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)

    assert len(trades) == 1
    assert trades[0].shares == 1000
    assert "000001.SZ" not in account.holdings


def test_add_to_existing_weighted_entry_price():
    holding = make_holding(shares=100, entry_price=9.0, last_price=10.0)
    account = _account(holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(open=10.0)}

    trades = rebalance_to_targets(account, bars, {"000001.SZ": 3000.0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)

    assert len(trades) == 1
    assert trades[0].side == "BUY"
    assert trades[0].trigger == "TARGET"
    assert trades[0].shares == 200
    h = account.holdings["000001.SZ"]
    assert h.shares == 300
    assert h.cost == pytest.approx(900.0 + 10.0 * 200)
    assert h.entry_price == pytest.approx(h.cost / h.shares)
    assert h.locked is True  # 加仓当天整仓锁定（保守 T+1）


def test_unlisted_holding_untouched():
    holding = make_holding(symbol="000002.SZ", shares=100)
    account = _account(holdings={"000002.SZ": holding})
    bars = {"000001.SZ": make_bar(), "000002.SZ": make_bar()}

    rebalance_to_targets(account, bars, {"000001.SZ": 5000.0}, 10,
                         get_limit_prices, calc_trade_costs, apply_slippage)

    assert account.holdings["000002.SZ"].shares == 100
    assert account.holdings["000002.SZ"].cost == 1000.0


def test_max_positions_limits_new_symbols():
    account = _account()
    bars = {s: make_bar() for s in ("000001.SZ", "000002.SZ", "000003.SZ")}
    targets = {s: 5000.0 for s in bars}

    trades = rebalance_to_targets(account, bars, targets, 2,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)

    assert len(account.holdings) == 2
    assert len(trades) == 2


def test_insufficient_cash_buys_less():
    account = _account(cash=1500.0)
    bars = {"000001.SZ": make_bar(open=10.0)}

    trades = rebalance_to_targets(account, bars, {"000001.SZ": 5000.0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)

    assert len(trades) == 1
    assert trades[0].shares == 100  # 现金只够 100 股
    assert account.cash >= 0


# ── Engine 端到端 ──


class TargetValueStrategy:
    """每日把 000001.SZ 调到固定目标市值。"""

    def __init__(self, config=None, mix_buy=False):
        self.config = config or {"slippage_ticks": 0, "max_positions": 10}
        self.mix_buy = mix_buy

    def get_universe(self, provider, start, end):
        return ["000001.SZ"]

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        actions = {"buy": [], "sell": [],
                   "target_value": {"000001.SZ": 50000.0}}
        if self.mix_buy:
            actions["buy"] = ["000001.SZ"]
        return actions

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_engine_target_value_e2e():
    provider = DataProvider(MockDataBackend())
    strategy = TargetValueStrategy()
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    result = engine.run("20240603", "20240607")

    trade_log = result["trade_log"]
    assert len(trade_log) > 0
    assert (trade_log["trigger"] == "TARGET").all()
    # 期末账户恒等
    acct = engine.account
    holdings_value = sum(h.shares * h.last_price
                         for h in acct.holdings.values())
    assert math.isclose(acct.cash + holdings_value, acct.total_value,
                        rel_tol=1e-6)
    assert "000001.SZ" in acct.holdings


def test_target_value_mutex_with_buy_sell():
    provider = DataProvider(MockDataBackend())
    strategy = TargetValueStrategy(mix_buy=True)
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    with pytest.raises(ValueError, match="互斥"):
        engine.run("20240603", "20240607")
