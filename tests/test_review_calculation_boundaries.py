"""F-MAT-02 / F-ENG-02：成交边界与缺行情日的独立回归。"""

import pandas as pd
import pytest

from btcore.backend import DataBackend
from btcore.costs import calc_trade_costs
from btcore.engine import Engine
from btcore.limits import get_limit_prices
from btcore.match.conditions import entry_conditions, exit_conditions
from btcore.match.manual import manual_buy, manual_sell, rebalance_to_targets
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage
from btcore.strategy import Strategy
from tests.conftest import make_account, make_bar, make_holding

SYMBOL = "000001.SZ"


@pytest.mark.parametrize("path", ["manual", "target", "condition"])
@pytest.mark.parametrize("side", ["BUY", "SELL"])
@pytest.mark.parametrize("distance", [0.01, 0.02, 0.03])
def test_slippage_limits_before_account_mutation(path, side, distance):
    boundary = 11.0 if side == "BUY" else 9.0
    direction = 1 if side == "BUY" else -1
    price = round(boundary - direction * distance, 2)
    bar = make_bar(open=price, high=11.0, low=9.0, pre_close=10.0,
                   up_limit=11.0, down_limit=9.0)
    holdings = {} if side == "BUY" else {SYMBOL: make_holding(shares=100)}
    # 条件单显式 ticks=2，账户 ticks=0，钉住条件单独立滑点的消费。
    account = make_account(cash=10000.0, holdings=holdings,
                           slippage_ticks=0 if path == "condition" else 2)
    bars = {SYMBOL: bar}
    if path == "manual":
        if side == "BUY":
            trades = manual_buy(account, bars, [SYMBOL], 1, get_limit_prices,
                                calc_trade_costs, apply_slippage)
        else:
            trades = manual_sell(account, bars, [SYMBOL], get_limit_prices,
                                 calc_trade_costs, apply_slippage)
    elif path == "target":
        trades = rebalance_to_targets(account, bars, {SYMBOL: 1200.0 if side == "BUY" else 0},
                                      1, get_limit_prices, calc_trade_costs, apply_slippage)
    elif side == "BUY":
        orders = [{"symbol": SYMBOL, "type": "BREAKOUT_BUY", "price": price, "shares": 100}]
        trades = entry_conditions(account, bars, orders, 1, get_limit_prices,
                                  calc_trade_costs, apply_slippage, slip_ticks=2)
    else:
        holdings[SYMBOL].conditions = [{"type": "STOP_LOSS", "price": price}]
        trades = exit_conditions(account, bars, get_limit_prices, calc_trade_costs,
                                 apply_slippage, slip_ticks=2)

    if distance <= 0.02:
        assert trades == []
        assert account.cash == 10000.0
        assert set(account.holdings) == (set() if side == "BUY" else {SYMBOL})
        if side == "SELL":
            assert account.holdings[SYMBOL].shares == 100
    else:
        assert len(trades) == 1
        trade = trades[0]
        assert trade.price == (10.99 if side == "BUY" else 9.01)
        value = trade.price * trade.shares
        commission = max(value * 0.00015, 5.0)
        transfer = value * 0.00001
        stamp = value * 0.0005 if side == "SELL" else 0.0
        expected_net = -(value + commission + transfer) if side == "BUY" else (
            value - commission - transfer - stamp)
        assert trade.net_amount == pytest.approx(expected_net)
        assert trade.slippage_amount == pytest.approx(0.02 * trade.shares)
        assert account.cash == pytest.approx(10000.0 + expected_net)


class MissingDayBackend(DataBackend):
    def __init__(self, stk_div=0.0):
        self.stk_div = stk_div

    def query_bars(self, symbols, start, end, columns=None):
        rows = [dict(trade_date=d, symbol=SYMBOL, open=10.0, close=10.0,
                     high=10.0, low=10.0, pre_close=10.0, adj_factor=1.0,
                     vol=10000.0, up_limit=11.0, down_limit=9.0)
                for d in ["20240531", "20240603", "20240605"] if start <= d <= end]
        return pd.DataFrame(rows).set_index(["trade_date", "symbol"])

    def get_calendar(self, start, end):
        return [d for d in ["20240531", "20240603", "20240604", "20240605"]
                if start <= d <= end]

    def get_dividends_on_date(self, date_str):
        return {SYMBOL: {"cash_div": 0.5, "stk_div": self.stk_div}} if (
            date_str == "20240604"
        ) else {}


class MissingDayStrategy(Strategy):
    def __init__(self, mode="sell"):
        super().__init__({"initial_capital": 10000.0, "max_positions": 10,
                          "slippage_ticks": 0})
        self.mode = mode
        self.decision_dates = []

    def select(self, bars, snapshot, provider):
        d = provider.get_as_of()
        self.decision_dates.append(d)
        if d == "20240531":
            return {"buy": [SYMBOL], "sell": []}
        if d == "20240603":
            if self.mode == "sell":
                return {"buy": [], "sell": [SYMBOL]}
            if self.mode == "target":
                return {"target_value": {SYMBOL: 0}}
            if self.mode == "condition":
                return {"buy_conditions": [{"symbol": "000002.SZ", "type": "LIMIT_BUY",
                                            "price": 10.0, "shares": 100}]}
        return {"buy": [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return [{"type": "TAKE_PROFIT", "price": 10.0}] if self.mode == "exit_condition" else []


@pytest.mark.parametrize("mode", ["sell", "target", "condition", "exit_condition"])
@pytest.mark.parametrize("stk_div", [0.0, 0.4])
def test_missing_day_settles_corporate_and_expires_orders(mode, stk_div):
    strategy = MissingDayStrategy(mode)
    engine = Engine(strategy, DataProvider(MissingDayBackend(stk_div)), db_path=":memory:")
    # 在缺行情日结束，直接观察待处理指令，而非依赖下一天的重算覆盖。
    result = engine.run("20240603", "20240604")
    daily = result["account_daily"]
    assert daily["date"].tolist() == ["20240603", "20240604"]
    # 独立手算：100 股 × 每股 0.5 × (1 - 20%)，送转不放大本次派现基数。
    assert result["trade_log"].query("side == 'DIV'")["net_amount"].sum() == 40.0
    assert daily.iloc[1]["cash"] - daily.iloc[0]["cash"] == 40.0
    assert result["statistics"]["total_days"] == 2
    assert strategy.decision_dates == ["20240531", "20240603"]
    assert engine.pending_actions == {"buy": [], "sell": []}
    holding = engine.account.holdings[SYMBOL]
    assert holding.shares == (100 if stk_div == 0.0 else 140)
    assert holding.holding_days == 2
    assert not holding.locked
    assert holding.conditions == []
    assert daily.iloc[1]["total_value"] == pytest.approx(
        daily.iloc[1]["cash"] + holding.shares * holding.last_price)


def test_missing_day_stale_sell_does_not_execute_after_resume():
    engine = Engine(MissingDayStrategy(), DataProvider(MissingDayBackend()), db_path=":memory:")
    result = engine.run("20240603", "20240605")
    assert result["trade_log"].query("side == 'SELL'").empty
    assert result["account_daily"]["date"].tolist() == ["20240603", "20240604", "20240605"]
    assert engine.account.holdings[SYMBOL].holding_days == 3


def test_missing_day_prefix_matches_longer_run():
    short = Engine(MissingDayStrategy(), DataProvider(MissingDayBackend()), db_path=":memory:")
    long = Engine(MissingDayStrategy(), DataProvider(MissingDayBackend()), db_path=":memory:")
    a = short.run("20240603", "20240604")
    b = long.run("20240603", "20240605")
    for key in ["account_daily", "trade_log"]:
        pd.testing.assert_frame_equal(a[key], b[key][b[key]["date"] <= "20240604"])
