"""order_volume_ratio 成交量约束测试。"""

from btcore.costs import calc_trade_costs
from btcore.engine import Engine
from btcore.limits import get_limit_prices
from btcore.match.core import cap_by_volume
from btcore.match.manual import manual_buy, manual_sell
from btcore.slippage import apply_slippage
from tests.conftest import make_account, make_bar, make_holding


def _account(cash=100_000.0, ratio=0.1, holdings=None):
    return make_account(cash=cash, holdings=holdings, slippage_ticks=0,
                        order_volume_ratio=ratio)


def test_buy_capped_by_volume():
    account = _account(ratio=0.1)
    # vol 单位为手（契约口径），50 手 = 5000 股
    bars = {"000001.SZ": make_bar(vol=50.0)}
    # 无约束可买 1000 股；vol*ratio=5 手 = 500 股 → 截断为 500
    trades = manual_buy(account, bars, ["000001.SZ"], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert len(trades) == 1
    assert trades[0].shares == 500
    assert trades[0].shares <= int(50.0 * 0.1) * 100


def test_sell_truncated_keeps_remainder():
    holding = make_holding()
    account = _account(cash=0.0, ratio=0.1,
                       holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(vol=50.0)}

    trades = manual_sell(account, bars, ["000001.SZ"],
                         get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].shares == 500
    h = account.holdings["000001.SZ"]
    assert h.shares == 500
    assert h.cost == 5000.0


def test_ratio_none_no_cap():
    holding = make_holding()
    account = _account(cash=0.0, ratio=None,
                       holdings={"000001.SZ": holding})
    bars = {"000001.SZ": make_bar(vol=50.0)}

    trades = manual_sell(account, bars, ["000001.SZ"],
                         get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].shares == 1000
    assert "000001.SZ" not in account.holdings


def test_cap_by_volume_missing_vol():
    account = _account(ratio=0.1)
    bar = make_bar()
    del bar["vol"]
    assert cap_by_volume(bar, 1000, account) == 1000


def test_engine_config_wires_ratio():
    class _S:
        def __init__(self, config):
            self.config = config

    engine = Engine(_S({"order_volume_ratio": 0.05}), None,
                    initial_capital=100_000)
    assert engine.account.order_volume_ratio == 0.05
    engine2 = Engine(_S({}), None, initial_capital=100_000)
    assert engine2.account.order_volume_ratio is None
