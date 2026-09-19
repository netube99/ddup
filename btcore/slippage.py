"""滑点函数：A 股股票最小变动价位 0.01 元，滑点价四舍五入到分。"""

from btcore.constants import TICK_SIZE


def apply_slippage(price: float, ticks: int, direction: int) -> float:
    """滑点价：price ± ticks × 0.01，四舍五入到分。"""
    return round(price + direction * ticks * TICK_SIZE, 2)
