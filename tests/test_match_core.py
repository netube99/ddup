from btcore.costs import calc_trade_costs
from btcore.match.core import (
    LIMIT_UNKNOWN,
    check_tradable,
    execute_buy,
    execute_sell,
)
from btcore.slippage import apply_slippage
from tests.conftest import make_account, make_bar, make_holding

NAN = float("nan")


def test_check_tradable_nan_limits_rejected():
    """F-MAT-03: 涨跌停 NaN 与 None 同样无法判定，买卖双双拒单。

    NaN 与任何价格比较恒 False，若放行会绕开封板校验静默成交。
    """
    assert check_tradable("BUY", 10.0, NAN, 10.2) == LIMIT_UNKNOWN
    assert check_tradable("SELL", 10.0, 10.2, NAN) == LIMIT_UNKNOWN
    assert check_tradable("BUY", 10.0, None, 10.2) == LIMIT_UNKNOWN
    # 有限涨跌停不受影响：正常买卖可成交、封板拒单
    assert check_tradable("BUY", 10.0, 11.0, 9.0) is None
    assert check_tradable("BUY", 11.0, 11.0, 9.0) == "LIMIT_UP"
    assert check_tradable("SELL", 9.0, 11.0, 9.0) == "LIMIT_DOWN"


def test_execute_sell():
    account = make_account(cash=5000.0)
    holding = make_holding(shares=100)
    bar = make_bar()

    trade = execute_sell(account, holding, bar, bar["open"], "MANUAL",
                         calc_trade_costs, apply_slippage)

    assert trade.side == "SELL"
    assert trade.shares == 100
    assert trade.trigger == "MANUAL"

    assert account.cash > 5000.0


def test_execute_buy():
    account = make_account()
    bar = make_bar()

    trade = execute_buy(account, "000001.SZ", bar, 100, bar["open"], "MANUAL",
                        calc_trade_costs, apply_slippage)

    assert trade.side == "BUY"
    assert trade.shares == 100
    assert trade.trigger == "MANUAL"
    assert account.cash < 100_000.0


def test_trade_fields_non_null():
    account = make_account()
    bar = make_bar()

    trade = execute_buy(account, "000001.SZ", bar, 100, bar["open"], "MANUAL",
                        calc_trade_costs, apply_slippage)

    assert trade.commission > 0
    assert trade.stamp_tax == 0
    assert trade.transfer_fee > 0
    assert trade.slippage_amount > 0
    # D8: BUY net_amount 为负 (现金流流出)
    assert trade.net_amount < 0
    assert trade.turnover == 1000.0
