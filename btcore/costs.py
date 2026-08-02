import math

from btcore.constants import COMMISSION_RATE, MIN_COMMISSION, STAMP_TAX_RATE, TRANSFER_FEE_RATE


def make_costs_fn(config: dict):
    """按 config 生成成本函数；缺省回退到 constants 硬编码费率。

    config 键：commission_rate / min_commission / stamp_tax_rate /
    transfer_fee_rate。引擎经本函数接线，策略/YAML 可覆盖费率。
    """
    commission_rate = float(config.get("commission_rate", COMMISSION_RATE))
    min_commission = float(config.get("min_commission", MIN_COMMISSION))
    stamp_tax_rate = float(config.get("stamp_tax_rate", STAMP_TAX_RATE))
    transfer_fee_rate = float(config.get("transfer_fee_rate", TRANSFER_FEE_RATE))

    # 2026-08 审计补校验：负费率/NaN 此前静默（stamp_tax_rate=-1 直接虚增卖出净额）
    for key, val in (("commission_rate", commission_rate),
                     ("min_commission", min_commission),
                     ("stamp_tax_rate", stamp_tax_rate),
                     ("transfer_fee_rate", transfer_fee_rate)):
        if not math.isfinite(val) or val < 0:
            raise ValueError(f"{key} 必须是非负有限数值: {val!r}")

    def calc(side: str, turnover: float) -> dict:
        commission = max(turnover * commission_rate, min_commission)
        stamp_tax = turnover * stamp_tax_rate if side == "SELL" else 0.0
        transfer_fee = turnover * transfer_fee_rate
        return {
            "commission": commission,
            "stamp_tax": stamp_tax,
            "transfer_fee": transfer_fee,
        }

    return calc


def calc_trade_costs(side: str, turnover: float) -> dict:
    commission = max(turnover * COMMISSION_RATE, MIN_COMMISSION)
    stamp_tax = turnover * STAMP_TAX_RATE if side == "SELL" else 0.0
    transfer_fee = turnover * TRANSFER_FEE_RATE
    return {
        "commission": commission,
        "stamp_tax": stamp_tax,
        "transfer_fee": transfer_fee,
    }
