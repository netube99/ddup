from btcore.costs import calc_trade_costs
from btcore.limits import get_limit_prices
from btcore.match.manual import manual_buy, manual_sell
from btcore.slippage import apply_slippage
from tests.conftest import make_account, make_bar, make_holding


def test_manual_sell_simple():
    holding = make_holding(shares=100)
    account = make_account(cash=5000.0, holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar()}

    trades = manual_sell(account, bars, ["000001.SZ"],
                         get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].side == "SELL"
    assert "000001.SZ" not in account.holdings


def test_manual_buy_simple():
    account = make_account()
    bars = {"000001.SZ": make_bar()}

    trades = manual_buy(account, bars, ["000001.SZ"], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].side == "BUY"
    assert "000001.SZ" in account.holdings


def test_manual_buy_insufficient_shares_skip():
    account = make_account(cash=500.0)
    bars = {"000001.SZ": make_bar(open=100.0)}

    trades = manual_buy(account, bars, ["000001.SZ"], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 0


def test_manual_buy_lot_size():
    account = make_account()
    bars = {"000001.SZ": make_bar()}

    trades = manual_buy(account, bars, ["000001.SZ"], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)

    assert trades[0].shares % 100 == 0
