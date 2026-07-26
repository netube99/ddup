from btcore.constants import TICK_SIZE


def apply_slippage(price: float, ticks: int, direction: int) -> float:
    return round(price + direction * ticks * TICK_SIZE, 2)
