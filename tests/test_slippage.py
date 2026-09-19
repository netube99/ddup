"""滑点函数：A 股股票 tick 0.01，滑点价四舍五入到分。"""

from btcore.slippage import apply_slippage


def test_slippage_grid():
    assert apply_slippage(10.0, 2, 1) == 10.02
    assert apply_slippage(10.0, 2, -1) == 9.98
    assert apply_slippage(9.101, 2, 1) == 9.12  # round 2 位
    assert apply_slippage(10.0, 0, 1) == 10.0
