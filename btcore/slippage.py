from decimal import Decimal

from btcore.constants import TICK_SIZE


def tick_decimals(tick_size: float) -> int:
    """tick 的小数位数（0.01 → 2，0.001 → 3），用于滑点舍入到 tick 网格。"""
    return max(0, -Decimal(str(tick_size)).as_tuple().exponent)


def apply_slippage(price: float, ticks: int, direction: int,
                   tick_size: float = TICK_SIZE) -> float:
    """滑点价：price ± ticks × tick_size，按 tick 网格舍入。

    tick_size 是品种最小变动价位（A 股股票 0.01，场内 ETF 0.001）。
    默认保持股票口径；ETF 策略经 config.tick_size 声明。
    """
    return round(price + direction * ticks * tick_size, tick_decimals(tick_size))
