"""btcore.strategy_tools.ConditionBuilder 测试。"""

from btcore.strategy_tools import ConditionBuilder


def _bar(close):
    return {"close": close}


def test_empty_rules():
    cond = ConditionBuilder({})
    assert cond.calc("A", 10.0, _bar(9.0), 1) == []


def test_stop_loss_and_take_profit():
    cond = ConditionBuilder({"stop_loss_pct": 0.08, "take_profit_pct": 0.25})
    conds = cond.calc("A", 10.0, _bar(10.0), 1)
    by_type = {c["type"]: c["price"] for c in conds}
    assert by_type["STOP_LOSS"] == 10.0 * 0.92
    assert by_type["TAKE_PROFIT"] == 10.0 * 1.25


def test_trailing_tracks_highest():
    cond = ConditionBuilder({"trailing_pct": 0.10})
    c1 = cond.calc("A", 10.0, _bar(10.0), 1)
    assert c1[0]["price"] == 10.0 * 0.9
    # 涨到 12，移动止盈线上移
    c2 = cond.calc("A", 10.0, _bar(12.0), 2)
    assert c2[0]["price"] == 12.0 * 0.9
    # 回落到 11，最高价仍为 12，线不动
    c3 = cond.calc("A", 10.0, _bar(11.0), 3)
    assert c3[0]["price"] == 12.0 * 0.9


def test_trailing_entry_floor():
    """最高价初始不低于成本价（bar 缺失 close 时用成本价）。"""
    cond = ConditionBuilder({"trailing_pct": 0.10})
    conds = cond.calc("A", 10.0, {}, 1)
    assert conds[0]["price"] == 10.0 * 0.9


def test_prune_clears_state():
    cond = ConditionBuilder({"trailing_pct": 0.10})
    cond.calc("A", 10.0, _bar(12.0), 1)
    cond.prune([])  # A 已平仓
    # 再次买入同一标的，最高价从成本价重新开始
    conds = cond.calc("A", 20.0, _bar(20.0), 1)
    assert conds[0]["price"] == 20.0 * 0.9


def test_prune_keeps_live_symbols():
    cond = ConditionBuilder({"trailing_pct": 0.10})
    cond.calc("A", 10.0, _bar(12.0), 1)
    cond.prune(["A"])
    conds = cond.calc("A", 10.0, _bar(11.0), 2)
    assert conds[0]["price"] == 12.0 * 0.9
