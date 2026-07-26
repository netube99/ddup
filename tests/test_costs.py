from btcore.costs import calc_trade_costs, make_costs_fn


def test_buy_costs():
    costs = calc_trade_costs("BUY", 10000.0)
    assert costs["commission"] == max(10000 * 0.00015, 5.0)
    assert costs["stamp_tax"] == 0.0
    assert costs["transfer_fee"] == 10000 * 0.00001


def test_sell_costs():
    costs = calc_trade_costs("SELL", 10000.0)
    assert costs["stamp_tax"] == 10000 * 0.0005


def test_min_commission():
    costs = calc_trade_costs("BUY", 1000.0)
    assert costs["commission"] == 5.0


def test_large_turnover():
    costs = calc_trade_costs("BUY", 1000000.0)
    assert costs["commission"] == 1000000.0 * 0.00015


def test_make_costs_fn_defaults():
    """空 config 与模块级 calc_trade_costs 行为一致。"""
    fn = make_costs_fn({})
    for side in ("BUY", "SELL"):
        assert fn(side, 10000.0) == calc_trade_costs(side, 10000.0)


def test_make_costs_fn_custom_rates():
    fn = make_costs_fn({"commission_rate": 0.001, "transfer_fee_rate": 0.0})
    costs = fn("BUY", 100000.0)
    assert costs["commission"] == 100000.0 * 0.001
    assert costs["transfer_fee"] == 0.0


def test_make_costs_fn_custom_min_commission():
    fn = make_costs_fn({"min_commission": 1.0})
    costs = fn("BUY", 1000.0)
    assert costs["commission"] == 1.0


def test_make_costs_fn_stamp_tax_sell_only():
    fn = make_costs_fn({"stamp_tax_rate": 0.001})
    assert fn("BUY", 10000.0)["stamp_tax"] == 0.0
    assert fn("SELL", 10000.0)["stamp_tax"] == 10000.0 * 0.001
