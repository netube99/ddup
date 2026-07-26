from btcore.costs import calc_trade_costs
from btcore.match.core import execute_buy, execute_sell
from btcore.slippage import apply_slippage
from tests.conftest import make_account, make_bar, make_holding


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
