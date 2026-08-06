"""公司行为处理 — 除权除息。

简化模型: 红利税在除息日按当时持股时长预扣 (≤30天 20% / ≤1年 10% / >1年 免征)，
真实规则为卖出时按最终持有期补扣。此处偏保守 (可能早扣/多扣)，方向安全。
"""

import logging
from datetime import date

from btcore import types
from btcore.types import bar_get

logger = logging.getLogger(__name__)


def derived_trades(corporate_log: list) -> list[types.Trade]:
    """公司行为日志 → 衍生 trade_log 行（回测落库与实盘衍生表重写共用）。

    送转增股必须落库：stats 往返盈亏 / Brinson 持仓重建 /
    ML 回合配对都从 trade_log 重建持股事实，缺失会腐化三处。
    """
    out = []
    for event in corporate_log:
        if event["type"] == "cash_div":
            out.append(types.Trade(
                date=event["date"], symbol=event["symbol"], side="DIV",
                trigger="CORPORATE", price=0.0, shares=0,
                turnover=0.0, commission=0.0, stamp_tax=0.0,
                transfer_fee=0.0, slippage_amount=0.0,
                net_amount=event["net"], reason="cash_div",
            ))
        elif event["type"] == "stk_div":
            out.append(types.Trade(
                date=event["date"], symbol=event["symbol"], side="STK_DIV",
                trigger="CORPORATE", price=0.0, shares=event["new_shares"],
                turnover=0.0, commission=0.0, stamp_tax=0.0,
                transfer_fee=0.0, slippage_amount=0.0,
                net_amount=0.0, reason="stk_div",
            ))
    return out


def apply_condition_rescale(strategy, corporate_log: list):
    """除权除息后同步 rescale 策略侧 trailing 锚点（S-COND-01，回测/实盘共用）。

    2026-08 实证：漏 rescale 时次日 calc_conditions 用除权前高点
    重算 TRAILING_TP 触发价，开盘即误触发卖出。
    """
    rescale = getattr(getattr(strategy, "_cond", None), "rescale", None)
    if rescale is None:
        return
    for entry in corporate_log:
        scale = entry.get("scale")
        if scale is not None:
            rescale(entry["symbol"], scale)


def adjust(account, today: str, day_bars, provider, log: list):
    dividends = provider.get_dividends_on_date(today)
    if not dividends:
        return

    for holding in list(account.holdings.values()):
        symbol = holding.symbol
        if symbol not in dividends:
            continue

        div = dividends[symbol]
        if div.get("stk_div", 0) > 0:
            _apply_stk_div(holding, div["stk_div"], today, log)
        if div.get("cash_div", 0) > 0:
            bar = day_bars.get(symbol)
            _apply_cash_div(account, holding, div["cash_div"], today, bar, log)


def _apply_stk_div(holding, stk_div: float, today: str, log: list):
    scale = 1.0 / (1.0 + stk_div)
    old_shares = holding.shares
    holding.shares = max(1, int(holding.shares * (1 + stk_div)))
    _rescale_holding(holding, scale)
    log.append({
        "date": today, "symbol": holding.symbol,
        "type": "stk_div", "stk_div": stk_div,
        "old_shares": old_shares, "new_shares": holding.shares,
        "scale": scale,
    })


def _apply_cash_div(account, holding, cash_div: float, today: str,
                    bar, log: list):
    gross = cash_div * holding.shares
    holding_days = (date.fromisoformat(today) - date.fromisoformat(holding.entry_date)).days
    if holding_days <= 30:
        tax_rate = 0.20
    elif holding_days <= 365:
        tax_rate = 0.10
    else:
        tax_rate = 0.0
    net = gross * (1 - tax_rate)
    account.cash += net
    holding.cost = max(0.0, holding.cost - net)

    pre_close = bar_get(bar, "pre_close", 0.0)
    if pre_close > 0:
        scale = pre_close / (pre_close + cash_div)
        _rescale_holding(holding, scale)
    else:
        # EDGE-12：缺 bar（bar=None）或 pre_close<=0 时 scale=None——cost 照减但
        # entry_price 与条件锚点不 rescale（apply_condition_rescale 静默跳过），
        # 数据洞必须显式告警（正常引擎流程每日每 symbol 至多告警一次）
        scale = None
        logger.warning(
            "cash_div %s %s 缺 bar 或 pre_close<=0（bar=%r），scale=None："
            "entry_price/条件锚点不 rescale",
            today, holding.symbol,
            None if bar is None else "pre_close<=0",
        )

    log.append({
        "date": today, "symbol": holding.symbol,
        "type": "cash_div", "gross": gross, "tax_rate": tax_rate,
        "net": net, "scale": scale,
    })


def _rescale_holding(holding, scale: float):
    holding.entry_price *= scale
    holding.last_price *= scale
    for c in holding.conditions:
        if "price" in c and c["price"] is not None:
            c["price"] *= scale
        if "trigger_price" in c:
            c["trigger_price"] *= scale
        if "drop_points" in c:
            c["drop_points"] *= scale
