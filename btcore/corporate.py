"""公司行为处理 — 除权除息。

简化模型: 红利税在除息日按当时持股时长预扣 (≤30天 20% / ≤1年 10% / >1年 免征)，
真实规则为卖出时按最终持有期补扣。此处偏保守 (可能早扣/多扣)，方向安全。
"""

import logging
from datetime import date

from btcore.types import bar_get

logger = logging.getLogger(__name__)


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

    log.append({
        "date": today, "symbol": holding.symbol,
        "type": "cash_div", "gross": gross, "tax_rate": tax_rate,
        "net": net,
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
