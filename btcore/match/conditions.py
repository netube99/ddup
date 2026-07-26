import logging

from btcore.match.core import (
    INVALID_PRICE,
    LIMIT_UNKNOWN,
    LIMIT_UP,
    apply_partial_sell,
    cap_by_volume,
    check_tradable,
    execute_buy,
    execute_sell,
    is_valid_price,
    make_holding,
    shrink_to_affordable,
)
from btcore.types import bar_get

logger = logging.getLogger(__name__)

_DISPATCH: dict = {}
_BUY_DISPATCH: dict = {}


def register_condition_handler(condition_type: str, handler):
    """注册条件卖出 handler: handler(holding, cond, bar) -> (executed, fill_price, log_params)。"""
    _DISPATCH[condition_type] = handler


def register_buy_condition_handler(condition_type: str, handler):
    """注册条件买入 handler: handler(order, bar) -> (executed, fill_price, log_params)。"""
    _BUY_DISPATCH[condition_type] = handler


def validate_condition_types(conditions: list[dict]) -> None:
    """检查每个 condition 的 type 是否已注册；未注册立即抛错。

    设计 §2.4 步 6: 未注册 type 在 _compute_pending 阶段快速失败,
    不等到次日撮合时才发现。
    """
    for cond in conditions:
        ctype = cond.get("type", "")
        if ctype not in _DISPATCH:
            raise ValueError(
                f"未注册的条件单类型: {ctype!r}, 已注册: {list(_DISPATCH)}"
            )


def validate_buy_condition_types(orders: list[dict]) -> None:
    """检查每个条件买入单的 type 是否已注册；未注册立即抛错。"""
    for order in orders:
        ctype = order.get("type", "")
        if ctype not in _BUY_DISPATCH:
            raise ValueError(
                f"未注册的条件买入类型: {ctype!r}, 已注册: {list(_BUY_DISPATCH)}"
            )


def exit_conditions(account, bars: dict, limits_fn, costs_fn, slip_fn,
                    quiet: bool = False,
                    slip_ticks: int | None = None) -> list:
    _warn = logger.debug if quiet else logger.warning
    trades = []
    for symbol, holding in list(account.holdings.items()):
        if holding.locked:
            continue
        bar = bars.get(symbol)
        if bar is None:
            continue

        trade_date = bar_get(bar, "trade_date", "")
        _, down = limits_fn(symbol, bar, trade_date)
        if down is None:
            _warn("[%s] %s 涨跌停无法判定, 跳过条件单",
                           trade_date, symbol)
            continue

        for cond in holding.conditions:
            # 防御：engine._compute_pending 已 validate_condition_types 前置校验，
            # 此分支仅在 match 层被绕过引擎直接调用时可达
            handler = _DISPATCH.get(cond.get("type", ""))
            if handler is None:
                raise ValueError(
                    f"未注册的条件单类型: {cond.get('type')} (symbol={symbol})"
                )
            executed, fill_price, log_params = handler(holding, cond, bar)
            if not executed:
                continue
            if not is_valid_price(fill_price):
                _warn(
                    "[%s] %s 条件单 %s 成交价非法 (%s), 顺延",
                    trade_date, symbol, cond["type"], fill_price)
                break
            if fill_price <= down:
                _warn(
                    "[%s] %s 跌停无法卖出, 条件单 %s 顺延 "
                    "(fill=%s limit_down=%s)",
                    trade_date, symbol, cond["type"], fill_price, down)
                break
            shares = cap_by_volume(bar, holding.shares, account)
            if shares < 100:
                _warn(
                    "[%s] %s 成交量约束下可卖不足 100 股, 条件单 %s 顺延",
                    trade_date, symbol, cond["type"])
                break
            trade = execute_sell(account, holding, bar, fill_price,
                                 cond["type"], costs_fn, slip_fn,
                                 shares=shares, slip_ticks=slip_ticks)
            trades.append(trade)
            logger.info("[%s] %s 条件单 %s 成交: fill=%s shares=%d %s",
                        trade_date, symbol, cond["type"], trade.price,
                        trade.shares, log_params)
            if shares >= holding.shares:
                del account.holdings[symbol]
            else:
                apply_partial_sell(holding, shares)
            break

    return trades


def handle_stop_loss(holding, cond: dict, bar) -> tuple:
    """固定止损: 价格跌到止损价触发。

    成交规则: open <= stop → fill at open;
             low <= stop 且 open > stop → fill at stop;
             否则不触发。显式 None 的 open/low 视为不触发。
    """
    stop_price = cond["price"]
    open_price = bar_get(bar, "open", 0.0)
    low_price = bar_get(bar, "low", open_price)

    if open_price is not None and open_price <= stop_price:
        return True, open_price, {"trigger_price": stop_price}
    if low_price is not None and low_price <= stop_price:
        return True, stop_price, {"trigger_price": stop_price}
    return False, 0.0, {"trigger_price": stop_price}


def handle_take_profit(holding, cond: dict, bar) -> tuple:
    """固定止盈: 价格涨到目标价触发。

    成交规则: open >= target → fill at open;
             high >= target 且 open < target → fill at target;
             否则不触发。显式 None 的 open/high 视为不触发。
    """
    target_price = cond["price"]
    open_price = bar_get(bar, "open", 0.0)
    high_price = bar_get(bar, "high", open_price)

    if open_price is not None and open_price >= target_price:
        return True, open_price, {"trigger_price": target_price}
    if high_price is not None and high_price >= target_price:
        return True, target_price, {"trigger_price": target_price}
    return False, 0.0, {"trigger_price": target_price}


register_condition_handler("STOP_LOSS", handle_stop_loss)
register_condition_handler("TAKE_PROFIT", handle_take_profit)
# TRAILING_TP 成交规则与 STOP_LOSS 相同: cond["price"] 由策略每日更新为
# highest_seen * (1 - trailing_pct)
register_condition_handler("TRAILING_TP", handle_stop_loss)


# ── 条件买入（T 日 select 声明, T+1 盘中触发, 单日有效）──


def entry_conditions(account, bars: dict, orders: list[dict],
                     max_positions: int, limits_fn, costs_fn, slip_fn,
                     quiet: bool = False,
                     slip_ticks: int | None = None) -> list:
    """条件买入撮合。约束与手动买一致：涨停不买、成交量 cap、现金不足减手数、
    max_positions 硬上限、成交即 T+1 锁定。已持仓标的不重复入场。"""
    _warn = logger.debug if quiet else logger.warning
    trades = []
    for order in orders:
        symbol = order["symbol"]
        if symbol in account.holdings:
            continue
        if len(account.holdings) >= max_positions:
            _warn("持仓数已达 max_positions=%d, 跳过条件买入 %s 及后续",
                           max_positions, symbol)
            break
        bar = bars.get(symbol)
        if bar is None:
            continue
        trade_date = bar_get(bar, "trade_date", "")

        # type 已在 engine._compute_pending 经 validate_buy_condition_types 校验
        handler = _BUY_DISPATCH[order["type"]]
        executed, fill_price, log_params = handler(order, bar)
        if not executed:
            continue

        up, down = limits_fn(symbol, bar, trade_date)
        reason = check_tradable("BUY", fill_price, up, down)
        if reason == LIMIT_UNKNOWN:
            _warn("[%s] %s 涨跌停无法判定, 跳过条件买入",
                           trade_date, symbol)
            continue
        if reason == INVALID_PRICE:
            _warn("[%s] %s 条件买入成交价非法 (%s), 跳过",
                           trade_date, symbol, fill_price)
            continue
        if reason == LIMIT_UP:
            _warn("[%s] %s 涨停不买, price=%s limit_up=%s",
                           trade_date, symbol, fill_price, up)
            continue

        if order.get("shares") is not None:
            shares = int(order["shares"] / 100) * 100
        else:
            shares = int(order["value"] / fill_price / 100) * 100
        shares = cap_by_volume(bar, shares, account)
        shares = shrink_to_affordable(account, shares, fill_price,
                                      costs_fn, slip_fn,
                                      slip_ticks=slip_ticks)
        if shares < 100:
            _warn("[%s] %s 条件买入可买不足 100 股, 跳过",
                           trade_date, symbol)
            continue

        trade = execute_buy(account, symbol, bar, shares, fill_price,
                            order["type"], costs_fn, slip_fn,
                            slip_ticks=slip_ticks)
        trades.append(trade)
        logger.info("[%s] %s 条件买入 %s 成交: fill=%s shares=%d %s",
                    trade_date, symbol, order["type"], trade.price,
                    trade.shares, log_params)
        account.holdings[symbol] = make_holding(symbol, bar, shares,
                                                trade.price)

    return trades


def handle_limit_buy(order: dict, bar) -> tuple:
    """限价买单: open <= price → 按 open 成交; 否则 low <= price → 按 price 成交。"""
    limit = order["price"]
    open_price = bar_get(bar, "open", 0.0)
    if not is_valid_price(open_price):
        return False, 0.0, {"trigger_price": limit}
    low_price = bar_get(bar, "low", open_price)

    if open_price <= limit:
        return True, open_price, {"trigger_price": limit}
    if low_price is not None and low_price <= limit:
        return True, limit, {"trigger_price": limit}
    return False, 0.0, {"trigger_price": limit}


def handle_breakout_buy(order: dict, bar) -> tuple:
    """突破买单: open >= price → 按 open 成交; 否则 high >= price → 按 price 成交。"""
    trigger = order["price"]
    open_price = bar_get(bar, "open", 0.0)
    if not is_valid_price(open_price):
        return False, 0.0, {"trigger_price": trigger}
    high_price = bar_get(bar, "high", open_price)

    if open_price >= trigger:
        return True, open_price, {"trigger_price": trigger}
    if high_price is not None and high_price >= trigger:
        return True, trigger, {"trigger_price": trigger}
    return False, 0.0, {"trigger_price": trigger}


register_buy_condition_handler("LIMIT_BUY", handle_limit_buy)
register_buy_condition_handler("BREAKOUT_BUY", handle_breakout_buy)
