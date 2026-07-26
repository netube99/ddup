import pytest

from btcore import limits
from btcore.costs import calc_trade_costs
from btcore.match.conditions import (
    exit_conditions,
    handle_stop_loss,
    handle_take_profit,
)
from btcore.slippage import apply_slippage
from tests.conftest import make_account, make_bar, make_holding


def test_stop_loss_open_triggers():
    holding = make_holding(shares=100,
                           conditions=[{"type": "STOP_LOSS", "price": 10.5}])
    bar = make_bar(low=9.5)

    executed, fill_price, _ = handle_stop_loss(holding, holding.conditions[0], bar)
    assert executed is True
    assert fill_price == 10.0


def test_stop_loss_low_triggers():
    holding = make_holding(shares=100,
                           conditions=[{"type": "STOP_LOSS", "price": 9.8}])
    bar = make_bar(low=9.5)

    executed, fill_price, _ = handle_stop_loss(holding, holding.conditions[0], bar)
    assert executed is True
    assert fill_price == 9.8


def test_stop_loss_not_triggered():
    holding = make_holding(shares=100,
                           conditions=[{"type": "STOP_LOSS", "price": 9.0}])
    bar = make_bar(low=9.5)

    executed, _, _ = handle_stop_loss(holding, holding.conditions[0], bar)
    assert executed is False


def test_locked_holding_skipped():
    holding = make_holding(shares=100, locked=True,
                           conditions=[{"type": "STOP_LOSS", "price": 10.5}])
    account = make_account(cash=5000.0, holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(low=9.5)}

    trades = exit_conditions(account, bars, limits.get_limit_prices,
                             calc_trade_costs, apply_slippage)
    assert len(trades) == 0


def test_unregistered_condition_raises():
    holding = make_holding(shares=100, locked=False,
                           conditions=[{"type": "UNKNOWN_TYPE", "price": 10.0}])
    account = make_account(cash=5000.0, holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(low=9.5)}

    with pytest.raises(ValueError, match="UNKNOWN_TYPE"):
        exit_conditions(account, bars, limits.get_limit_prices,
                        calc_trade_costs, apply_slippage)


def test_condition_sell_blocked_at_limit_down():
    """止损触发但成交价 <= 跌停价 → 不成交, 持仓保留, 条件单顺延。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "STOP_LOSS", "price": 9.5}])
    account = make_account(cash=5000.0, holdings={"000001.SZ": holding})
    # pre_close 10.0 → limit_down 9.0; 跳空跌停 open=9.0 <= stop 9.5,
    # fill=9.0 <= limit_down → 跌停卖不掉
    bar = make_bar(open=9.0, pre_close=10.0, up_limit=11.0, down_limit=9.0)

    trades = exit_conditions(account, {"000001.SZ": bar}, limits.get_limit_prices,
                             calc_trade_costs, apply_slippage)
    assert len(trades) == 0
    assert "000001.SZ" in account.holdings


def test_take_profit_open_triggers():
    """TAKE_PROFIT: open 已达目标价，以 open 成交。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "TAKE_PROFIT", "price": 12.0}])
    bar = make_bar(open=12.5, high=13.0, low=11.0)

    executed, fill_price, _ = handle_take_profit(holding, holding.conditions[0], bar)
    assert executed is True
    assert fill_price == 12.5


def test_take_profit_high_triggers():
    """TAKE_PROFIT: open 未到但盘中触及目标价，以目标价成交。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "TAKE_PROFIT", "price": 13.0}])
    bar = make_bar(open=12.0, high=13.5, low=11.5)

    executed, fill_price, _ = handle_take_profit(holding, holding.conditions[0], bar)
    assert executed is True
    assert fill_price == 13.0


def test_take_profit_not_triggered():
    """TAKE_PROFIT: 全天未触及目标价，不触发。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "TAKE_PROFIT", "price": 15.0}])
    bar = make_bar(open=12.0, high=14.0, low=11.0)

    executed, _, _ = handle_take_profit(holding, holding.conditions[0], bar)
    assert executed is False


def test_trailing_tp_open_triggers():
    """TRAILING_TP: 开盘即跌破止损线，以 open 成交。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "TRAILING_TP", "price": 11.0}])
    bar = make_bar(open=10.5, high=10.8, low=10.0)

    executed, fill_price, _ = handle_stop_loss(holding, holding.conditions[0], bar)
    assert executed is True
    assert fill_price == 10.5


def test_trailing_tp_low_triggers():
    """TRAILING_TP: 开盘未破但盘中跌破止损线，以止损价成交。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "TRAILING_TP", "price": 10.0}])
    bar = make_bar(open=10.5, high=11.0, low=9.5)

    executed, fill_price, _ = handle_stop_loss(holding, holding.conditions[0], bar)
    assert executed is True
    assert fill_price == 10.0


def test_trailing_tp_not_triggered():
    """TRAILING_TP: 全天未跌破止损线，不触发。"""
    holding = make_holding(shares=100,
                           conditions=[{"type": "TRAILING_TP", "price": 9.0}])
    bar = make_bar(high=11.0, low=9.5)

    executed, _, _ = handle_stop_loss(holding, holding.conditions[0], bar)
    assert executed is False


# ── 条件单独立滑点档数（slip_ticks 覆盖 account.slippage_ticks）──


def _stop_loss_account(ticks):
    holding = make_holding(shares=100,
                           conditions=[{"type": "STOP_LOSS", "price": 9.5}])
    return make_account(cash=5000.0, holdings={"000001.SZ": holding},
                        slippage_ticks=ticks)


def _stop_loss_bar():
    # open 未破止损价、盘中跌破 → 触发价 = 止损价 9.5
    return {"000001.SZ": make_bar(open=10.0, high=10.2, low=9.4)}


def test_exit_condition_slip_ticks_override():
    account = _stop_loss_account(ticks=5)

    trades = exit_conditions(account, _stop_loss_bar(), limits.get_limit_prices,
                             calc_trade_costs, apply_slippage, slip_ticks=1)

    assert len(trades) == 1
    assert trades[0].price == 9.49  # 9.5 - 1 档, 不用 account 的 5 档


def test_exit_condition_slip_ticks_zero():
    account = _stop_loss_account(ticks=5)

    trades = exit_conditions(account, _stop_loss_bar(), limits.get_limit_prices,
                             calc_trade_costs, apply_slippage, slip_ticks=0)

    assert trades[0].price == 9.5


def test_exit_condition_slip_ticks_default_falls_back():
    account = _stop_loss_account(ticks=2)

    trades = exit_conditions(account, _stop_loss_bar(), limits.get_limit_prices,
                             calc_trade_costs, apply_slippage)

    assert trades[0].price == 9.48  # 回退 account.slippage_ticks
