"""apply_slippage 舍入契约：四舍五入到分（Decimal HALF_UP，与 limits._round2_half_up 同口径）。

F-CST-01 回归：round() 是银行家舍入且受二进制浮点影响，dyadic 半分位
（x.125/.375/.625/.875）成交价偏 1 tick。
"""

from decimal import ROUND_HALF_UP, Decimal

import pytest

from btcore.slippage import apply_slippage


def _half_up(price: float, ticks: int, direction: int) -> float:
    moved = Decimal(repr(price)) + direction * ticks * Decimal("0.01")
    return float(moved.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


@pytest.mark.parametrize(
    "price,ticks,direction",
    [
        (8.625, 0, -1),  # dyadic 半分位：round() 半偶得 8.62，四舍五入应进位 8.63
        (8.375, 0, 1),  # 8.375+0=8.375 → 8.38（round() 半偶同为 8.38，边界对照）
        (9.625, 1, -1),  # 9.625-0.01=9.615 dyadic 半分位 → 9.62
        (10.7, 2, 1),  # 非半分位：10.70+0.02=10.72 无歧义
        (5.2725, 2, -1),  # 非半分位（十进制 5.2525）：两侧一致 5.25
        (10.0, 0, 1),  # ticks=0 原价
    ],
)
def test_apply_slippage_rounds_half_up(price, ticks, direction):
    assert apply_slippage(price, ticks, direction) == _half_up(price, ticks, direction)


def test_dyadic_half_cent_rounds_up_not_even():
    """8.625 卖出无滑点：round() 半偶向下 8.62，四舍五入契约要求 8.63。"""
    assert apply_slippage(8.625, 0, -1) == 8.63


def test_slippage_grid():
    assert apply_slippage(10.0, 2, 1) == 10.02
    assert apply_slippage(10.0, 2, -1) == 9.98
    assert apply_slippage(9.101, 2, 1) == 9.12  # HALF_UP 后仍 2 位
    assert apply_slippage(10.0, 0, 1) == 10.0
