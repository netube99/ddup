import math
from decimal import ROUND_HALF_UP, Decimal

from btcore.constants import PLATE_LIMIT_RULES
from btcore.types import bar_get


def get_limit_prices(symbol: str, bar, today: str) -> tuple:
    up_limit = bar_get(bar, "up_limit")
    down_limit = bar_get(bar, "down_limit")
    if not _missing(up_limit) and not _missing(down_limit):
        return up_limit, down_limit

    pre_close = bar_get(bar, "pre_close", 0.0)
    if _missing(pre_close) or pre_close <= 0:
        return None, None

    rate = _get_plate_rate(symbol, today)
    if rate is None:
        return None, None

    up = _round2_half_up(pre_close, 1 + rate)
    down = _round2_half_up(pre_close, 1 - rate)
    return up, down


def _round2_half_up(pre_close: float, factor: float) -> float:
    """交易所四舍五入到分的口径。

    用 Decimal(repr) 重算乘积，避免二进制浮点把 x.xx5 压小后
    被 round() 银行家舍入错方向（如 10.05×1.1=11.055 → 11.06 而非 11.05）。
    """
    exact = Decimal(repr(pre_close)) * Decimal(repr(factor))
    return float(exact.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def _missing(v) -> bool:
    """None 或 NaN（LEFT JOIN 缺行 / shift 产生的首日 pre_close）都视为缺失。"""
    return v is None or (isinstance(v, float) and math.isnan(v))


def _get_plate_rate(symbol: str, today: str) -> float | None:
    if symbol.endswith(".BJ"):
        return PLATE_LIMIT_RULES["BJ"]["rate"]
    if symbol.startswith("688"):
        return PLATE_LIMIT_RULES["688"]["rate"]
    prefix = symbol[:3]
    if prefix.startswith("30"):
        # 创业板按完整 3 位前缀查表 (300/301); 未收录的 30x 前缀回退 300 规则
        rule = PLATE_LIMIT_RULES.get(prefix, PLATE_LIMIT_RULES["300"])
        if today >= rule["switch_date"]:
            return rule["new_rate"]
        return rule["rate"]
    return PLATE_LIMIT_RULES["default"]["rate"]
