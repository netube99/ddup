"""滑点函数：股票 0.01 / ETF 0.001 两种 tick 口径与网格舍入。"""

import pytest

from btcore.slippage import apply_slippage, tick_decimals


def test_tick_decimals():
    assert tick_decimals(0.01) == 2
    assert tick_decimals(0.001) == 3
    assert tick_decimals(1) == 0


def test_stock_default_unchanged():
    """默认口径 = 股票 0.01：与历史行为逐值一致。"""
    assert apply_slippage(10.0, 2, 1) == 10.02
    assert apply_slippage(10.0, 2, -1) == 9.98
    assert apply_slippage(9.101, 2, 1) == 9.12  # round 2 位


def test_etf_mill_tick_grid():
    """ETF tick 0.001：滑点保留毫厘，不得被 round(2) 吃掉。"""
    assert apply_slippage(9.101, 2, 1, tick_size=0.001) == pytest.approx(9.103)
    assert apply_slippage(9.101, 2, -1, tick_size=0.001) == pytest.approx(9.099)
    assert apply_slippage(9.101, 1, 1, tick_size=0.001) == pytest.approx(9.102)
    # 0 档滑点 = 原价（不因舍入漂移）
    assert apply_slippage(9.101, 0, 1, tick_size=0.001) == pytest.approx(9.101)
