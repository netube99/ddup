import logging

from btcore.match.core import (
    _cash_affordable,
    _warn_skip_reason,
    apply_partial_sell,
    cap_by_volume,
    check_tradable,
    exec_price,
    execute_buy,
    execute_sell,
    is_valid_price,
    make_holding,
)
from btcore.types import bar_get

logger = logging.getLogger(__name__)


def manual_sell(account, bars: dict, sell_symbols: list,
                limits_fn, costs_fn, slip_fn,
                shares_map: dict | None = None,
                quiet: bool = False,
                trigger: str = "MANUAL",
                reasons_map: dict | None = None) -> list:
    """手动卖出。shares_map 为 None 时清仓（现状）；否则按指定股数部分卖出。

    trigger 透传进成交记录，缺省 "MANUAL"；reasons_map 可按 symbol 覆盖
    （select 协议 sell_reasons 键，用于卖出来源归因，如 TREND_BREAK）。
    """
    _warn = logger.debug if quiet else logger.warning
    trades = []
    for symbol in sell_symbols:
        if symbol not in account.holdings:
            # EDGE-06: 走 _warn（此前 logger.info 不响应 quiet_skips）
            _warn("卖出名单含未持仓标的 %s, 跳过", symbol)
            continue
        holding = account.holdings[symbol]
        bar = bars.get(symbol)
        if bar is None:
            _warn("%s 无当日行情（停牌/缺数据）, 跳过卖出", symbol)
            continue

        exec_px = exec_price(bar, account)
        trade_date = bar_get(bar, "trade_date", "")

        up, down = limits_fn(symbol, bar, trade_date)
        reason = check_tradable("SELL", exec_px, up, down)
        if reason is not None:
            _warn_skip_reason(reason, "SELL", _warn, trade_date, symbol)
            continue

        desired = holding.shares if shares_map is None else shares_map.get(
            symbol, holding.shares)
        shares = cap_by_volume(bar, min(desired, holding.shares), account)
        if shares < 100:
            _warn("[%s] %s 可卖股数不足 100 (受成交量约束), 跳过",
                           trade_date, symbol)
            continue

        trade = execute_sell(account, holding, bar, exec_px,
                             (reasons_map or {}).get(symbol, trigger),
                             costs_fn, slip_fn, shares=shares)
        trades.append(trade)
        if shares >= holding.shares:
            del account.holdings[symbol]
        else:
            if shares < desired:
                _warn("[%s] %s 成交量约束截断卖出: %d/%d",
                               trade_date, symbol, shares, desired)
            apply_partial_sell(holding, shares)

    return trades


def manual_buy(account, bars: dict, buy_symbols: list,
               max_positions: int, limits_fn, costs_fn, slip_fn,
               weights_map: dict | None = None,
               quiet: bool = False) -> list:
    """手动买入（仅新标的）。持仓数达 max_positions 后只记 INFO、不拦截。

    weights_map 为 None 时等权（总资产的 1/max_positions）；否则按
    总资产 × weights_map[symbol] 分配每笔买入金额。
    """
    if max_positions <= 0:
        return []
    _warn = logger.debug if quiet else logger.warning
    open_total_value = _calc_exec_total_value(account, bars)
    base_amount = open_total_value / max_positions

    eligible = [s for s in buy_symbols if s not in account.holdings]

    trades = []
    for idx, symbol in enumerate(eligible):
        n_left = len(eligible) - idx
        # 循环内实时复查（引擎 compute_pending 已查重，这里兜底）：
        # 名单重复或前一笔已成交时跳过，避免重复扣款 + 持仓覆盖
        if symbol in account.holdings:
            continue
        if len(account.holdings) >= max_positions:
            logger.info("持仓数已达 max_positions=%d, 继续买入 %s",
                        max_positions, symbol)
        bar = bars.get(symbol)
        if bar is None:
            _warn("%s 无当日行情（停牌/缺数据）, 跳过买入", symbol)
            continue

        exec_px = exec_price(bar, account)
        trade_date = bar_get(bar, "trade_date", "")

        up, down = limits_fn(symbol, bar, trade_date)
        reason = check_tradable("BUY", exec_px, up, down)
        if reason is not None:
            _warn_skip_reason(reason, "BUY", _warn, trade_date, symbol)
            continue

        if weights_map is not None:
            effective_amount = min(open_total_value * weights_map[symbol],
                                   account.cash)
        else:
            effective_amount = min(base_amount, account.cash / n_left)
        shares = int(effective_amount / exec_px / 100) * 100
        shares = cap_by_volume(bar, shares, account)
        if shares < 100:
            _warn("[%s] %s 买入金额不足 100 股, fill=%s 跳过",
                           trade_date, symbol, exec_px)
            continue

        affordable, est_net = _cash_affordable(
            account, exec_px, shares, slip_fn, costs_fn
        )
        if not affordable:
            _warn("[%s] %s 现金不足 (need=%.2f cash=%.2f) 跳过",
                           trade_date, symbol, est_net, account.cash)
            continue

        trade = execute_buy(account, symbol, bar, shares, exec_px, "MANUAL",
                            costs_fn, slip_fn)
        trades.append(trade)

        account.holdings[symbol] = make_holding(symbol, bar, shares, trade.price)

    return trades


def _calc_exec_total_value(account, bars: dict) -> float:
    value = account.cash
    for symbol, holding in account.holdings.items():
        bar = bars.get(symbol)
        price = exec_price(bar, account) if bar is not None else None
        value += holding.shares * (
            price if is_valid_price(price) else holding.last_price
        )
    return value


def rebalance_to_targets(account, bars: dict, targets: dict,
                         max_positions: int, limits_fn, costs_fn,
                         slip_fn, quiet: bool = False) -> list:
    """按目标市值调仓（trigger="TARGET"）。先卖后买释放现金。

    只调整出现在 targets 里的标的；未列出的持仓不动（不自动清仓），
    target=0 显式清仓：零碎股一并卖出（卖出不要求整手），且不受成交量
    cap 截断（EDGE-02：截断会残留持仓且当日不重试）。
    加仓按加权均价更新 entry_price；当天新买/加仓部分会把整个持仓
    锁一天（locked 是持仓级布尔，保守行为），次日统一解锁。
    """
    _warn = logger.debug if quiet else logger.warning
    trades = []
    sells, buys = [], []
    for symbol, target in targets.items():
        holding = account.holdings.get(symbol)
        current = 0.0
        bar = bars.get(symbol)
        if holding is not None:
            if bar is None:
                _warn("%s 无当日行情（停牌/缺数据）, 跳过调仓", symbol)
                continue
            price = exec_price(bar, account)
            current = holding.shares * (
                price if is_valid_price(price) else holding.last_price
            )
        diff = target - current
        if diff < 0 and holding is not None:
            sells.append((symbol, -diff, target <= 0))
        elif diff > 0:
            buys.append((symbol, diff))

    for symbol, amount, full_exit in sells:
        holding = account.holdings[symbol]
        bar = bars[symbol]
        exec_px = exec_price(bar, account)
        trade_date = bar_get(bar, "trade_date", "")

        up, down = limits_fn(symbol, bar, trade_date)
        reason = check_tradable("SELL", exec_px, up, down)
        if reason is not None:
            _warn_skip_reason(reason, "SELL", _warn, trade_date, symbol)
            continue

        if full_exit:
            # 显式清仓: 零碎股一并卖出（卖出不要求整手），不受成交量 cap
            # 截断（EDGE-02：截断后残留持仓且当日不重试）
            desired = holding.shares
            shares = desired
        else:
            desired = min(int(amount / exec_px / 100) * 100, holding.shares)
            shares = cap_by_volume(bar, desired, account)
            if shares < 100:
                _warn("[%s] %s 可卖股数不足 100 (受成交量约束), 跳过",
                               trade_date, symbol)
                continue

        trade = execute_sell(account, holding, bar, exec_px, "TARGET",
                             costs_fn, slip_fn, shares=shares)
        trades.append(trade)
        if shares >= holding.shares:
            del account.holdings[symbol]
        else:
            if shares < desired:
                _warn("[%s] %s 成交量约束截断卖出: %d/%d",
                               trade_date, symbol, shares, desired)
            apply_partial_sell(holding, shares)

    for symbol, amount in buys:
        holding = account.holdings.get(symbol)
        if holding is None and len(account.holdings) >= max_positions:
            logger.info("%s 持仓已达 max_positions=%d, 继续新买",
                        symbol, max_positions)
        bar = bars.get(symbol)
        if bar is None:
            _warn("%s 无当日行情（停牌/缺数据）, 跳过买入", symbol)
            continue

        exec_px = exec_price(bar, account)
        trade_date = bar_get(bar, "trade_date", "")

        up, down = limits_fn(symbol, bar, trade_date)
        reason = check_tradable("BUY", exec_px, up, down)
        if reason is not None:
            _warn_skip_reason(reason, "BUY", _warn, trade_date, symbol)
            continue

        shares = int(amount / exec_px / 100) * 100
        shares = cap_by_volume(bar, shares, account)
        if shares < 100:
            _warn("[%s] %s 目标加仓金额不足 100 股, 跳过",
                           trade_date, symbol)
            continue

        affordable, est_net = _cash_affordable(
            account, exec_px, shares, slip_fn, costs_fn
        )
        if not affordable:
            _warn("[%s] %s 现金不足 (need=%.2f cash=%.2f) 跳过",
                           trade_date, symbol, est_net, account.cash)
            continue

        trade = execute_buy(account, symbol, bar, shares, exec_px, "TARGET",
                            costs_fn, slip_fn)
        trades.append(trade)

        if holding is None:
            account.holdings[symbol] = make_holding(symbol, bar, shares,
                                                    trade.price)
        else:
            holding.shares += shares
            holding.cost += trade.price * shares
            holding.entry_price = holding.cost / holding.shares
            holding.last_price = trade.price
            holding.locked = True

    return trades
