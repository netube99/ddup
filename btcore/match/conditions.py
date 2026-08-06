import logging

from btcore.match.core import (
    LIMIT_UNKNOWN,
    _cash_affordable,
    _warn_skip_reason,
    apply_partial_sell,
    cap_by_volume,
    check_tradable,
    execute_buy,
    execute_sell,
    is_valid_price,
    make_holding,
)
from btcore.types import bar_get

logger = logging.getLogger(__name__)

_DISPATCH: dict = {}
_BUY_DISPATCH: dict = {}
# 条件单类型 → 必填键（validate_condition_types 提前校验，T 日 fail-fast）
_REQUIRED_KEYS: dict[str, frozenset[str]] = {}


def register_condition_handler(condition_type: str, handler, required_keys=None):
    """注册条件卖出 handler: handler(holding, cond, bar) -> (executed, fill_price, log_params)。

    required_keys: 该类型条件单 dict 的必填键（如 {"price"}），
    缺键会在 compute_pending 阶段立即报错，而不是拖到次日撮合时 KeyError。
    """
    _DISPATCH[condition_type] = handler
    if required_keys:
        _REQUIRED_KEYS[condition_type] = frozenset(required_keys)


def register_buy_condition_handler(condition_type: str, handler):
    """注册条件买入 handler: handler(order, bar) -> (executed, fill_price, log_params)。"""
    _BUY_DISPATCH[condition_type] = handler


def registered_condition_types() -> set[str]:
    """已注册的条件单类型（卖出 + 买入），供 cross_validate 等消费。"""
    return set(_DISPATCH) | set(_BUY_DISPATCH)


def validate_condition_types(conditions: list[dict]) -> None:
    """检查每个 condition 的 type 是否已注册、必填键是否齐全；未通过立即抛错。

    设计 §2.4 步 6: 未注册 type / 缺必填键在 compute_pending 阶段快速失败,
    不等到次日撮合时才发现（handler 直取 cond["price"] 会 KeyError 崩 run）。
    """
    for cond in conditions:
        ctype = cond.get("type", "")
        if ctype not in _DISPATCH:
            raise ValueError(
                f"未注册的条件单类型: {ctype!r}, 已注册: {list(_DISPATCH)}"
            )
        missing = _REQUIRED_KEYS.get(ctype, frozenset()) - set(cond)
        if missing:
            raise ValueError(
                f"条件单 {ctype} 缺必填键: {sorted(missing)} "
                f"(完整条件单: {cond!r})"
            )
        price = cond.get("price")
        if ctype in _REQUIRED_KEYS and not is_valid_price(price):
            raise ValueError(f"条件单 {ctype}.price 必须是正数: {price!r}")


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
            _warn("%s 无当日行情（停牌/缺数据）, 跳过条件单", symbol)
            continue
        # PERF-04: 空条件持仓不白算涨跌停（limits_fn 含 Decimal quantize，
        # 相对昂贵）；涨跌停判定也只在条件触发后才需要
        if not holding.conditions:
            continue
        trade_date = bar_get(bar, "trade_date", "")

        for cond in holding.conditions:
            # 防御：engine.compute_pending 已 validate_condition_types 前置校验，
            # 此分支仅在 match 层被绕过引擎直接调用时可达
            handler = _DISPATCH.get(cond.get("type", ""))
            if handler is None:
                raise ValueError(
                    f"未注册的条件单类型: {cond.get('type')} (symbol={symbol})"
                )
            executed, fill_price, log_params = handler(holding, cond, bar)
            if not executed:
                continue
            # 条件已触发，此时才需要 down（PERF-04）；LIMIT_UNKNOWN 语义
            # 保持：down 缺失时跳过该持仓（后续条件单同样无法判定）
            _, down = limits_fn(symbol, bar, trade_date)
            if down is None:
                _warn_skip_reason(
                    LIMIT_UNKNOWN, "SELL", _warn, trade_date, symbol
                )
                break
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
             否则不触发。缺 open/low 或价格非正视为不触发（EDGE-01：
             与 take_profit/limit_buy 缺键语义对齐——bar_get 回退 0.0 会让
             0 <= stop 恒真 → 每日"成交价非法顺延"误告警）。
    """
    stop_price = cond["price"]
    open_price = bar_get(bar, "open")
    low_price = bar_get(bar, "low")

    if (open_price is not None and is_valid_price(open_price)
            and open_price <= stop_price):
        return True, open_price, {"trigger_price": stop_price}
    if (low_price is not None and is_valid_price(low_price)
            and low_price <= stop_price):
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


register_condition_handler("STOP_LOSS", handle_stop_loss, required_keys={"price"})
register_condition_handler("TAKE_PROFIT", handle_take_profit, required_keys={"price"})
# TRAILING_TP 成交规则与 STOP_LOSS 相同: cond["price"] 由策略每日更新为
# highest_seen * (1 - trailing_pct)
register_condition_handler("TRAILING_TP", handle_stop_loss, required_keys={"price"})


# ── 条件买入（T 日 select 声明, T+1 盘中触发, 单日有效）──


def entry_conditions(account, bars: dict, orders: list[dict],
                     max_positions: int, limits_fn, costs_fn, slip_fn,
                     quiet: bool = False,
                     slip_ticks: int | None = None) -> list:
    """条件买入撮合。约束与手动买一致：涨停不买、成交量 cap、
    现金不足跳过、成交即 T+1 锁定。已持仓标的不重复入场。"""
    _warn = logger.debug if quiet else logger.warning
    if orders is None:
        return []
    trades = []
    for order in orders:
        symbol = order["symbol"]
        if symbol in account.holdings:
            continue
        if len(account.holdings) >= max_positions:
            logger.info("持仓数已达 max_positions=%d, 继续条件买入 %s",
                        max_positions, symbol)
        bar = bars.get(symbol)
        if bar is None:
            _warn("%s 无当日行情（停牌/缺数据）, 跳过条件买入", symbol)
            continue
        trade_date = bar_get(bar, "trade_date", "")

        # type 已在 engine.compute_pending 经 validate_buy_condition_types 校验
        handler = _BUY_DISPATCH[order["type"]]
        executed, fill_price, log_params = handler(order, bar)
        if not executed:
            continue

        up, down = limits_fn(symbol, bar, trade_date)
        reason = check_tradable("BUY", fill_price, up, down)
        if reason is not None:
            _warn_skip_reason(reason, "BUY", _warn, trade_date, symbol)
            continue

        if order.get("shares") is not None:
            shares = int(order["shares"] / 100) * 100
        else:
            shares = int(order["value"] / fill_price / 100) * 100
        shares = cap_by_volume(bar, shares, account)
        if shares < 100:
            _warn("[%s] %s 条件买入可买不足 100 股, 跳过",
                           trade_date, symbol)
            continue

        affordable, est_net = _cash_affordable(
            account, fill_price, shares, slip_fn, costs_fn,
            slip_ticks=slip_ticks,
        )
        if not affordable:
            _warn("[%s] %s 现金不足 (need=%.2f cash=%.2f) 跳过",
                           trade_date, symbol, est_net, account.cash)
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
