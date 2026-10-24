"""滑点函数：A 股股票最小变动价位 0.01 元，滑点价四舍五入到分。"""

from decimal import ROUND_HALF_UP, Decimal

from btcore.constants import TICK_SIZE


def apply_slippage(price: float, ticks: int, direction: int) -> float:
    """滑点价：price ± ticks × 0.01，四舍五入到分。

    Decimal(repr) + HALF_UP，与 limits._round2_half_up 同口径：避免二进制浮点
    把 x.xx5 压小后被 round() 银行家舍入错方向（dyadic 半分位偏 1 tick）。
    """
    moved = Decimal(repr(price)) + direction * ticks * Decimal(str(TICK_SIZE))
    return float(moved.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
