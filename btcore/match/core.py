from btcore.types import Holding, Trade, bar_get


def is_valid_price(p) -> bool:
    """None / NaN / 非正价格一律非法（数据空洞），撮合与结算必须拒绝。"""
    return p is not None and p == p and p > 0


def exec_price(bar, account):
    """手动单成交价字段（account.execution_price: open|close）的取价。

    字段缺失返回 None，由 is_valid_price 拒单；条件单有自己的触发模型，不走这里。
    """
    return bar_get(bar, getattr(account, "execution_price", "open"))


def _date(bar) -> str:
    return bar["trade_date"] if isinstance(bar, dict) else bar.trade_date


def make_holding(symbol: str, bar, shares: int, fill_price: float) -> Holding:
    """成交当日新建持仓（T+1 锁定）。"""
    return Holding(
        symbol=symbol,
        shares=shares,
        entry_date=_date(bar),
        entry_price=fill_price,
        cost=fill_price * shares,
        last_price=fill_price,
        locked=True,
    )


def shrink_to_affordable(account, shares: int, price: float,
                         costs_fn, slip_fn,
                         slip_ticks: int | None = None) -> int:
    """现金不足则逐步减 100 股直到费用估可承受；不足 100 股返回 <100 由调用方跳过。"""
    ticks = account.slippage_ticks if slip_ticks is None else slip_ticks
    while shares >= 100:
        est_price = slip_fn(price, ticks, 1)
        est_costs = costs_fn("BUY", est_price * shares)
        est_net = (est_price * shares + est_costs["commission"]
                   + est_costs["transfer_fee"])
        if account.cash >= est_net:
            break
        shares -= 100
    return shares


def cap_by_volume(bar, shares: int, account) -> int:
    """按 account.order_volume_ratio 截单笔股数（None 或 bar 无 vol → 原样）。

    vol 单位为手（1 手 = 100 股，tushare 原生口径，数据契约见
    docs/backend_guide.md），cap = int(vol * ratio) * 100 股。
    """
    ratio = getattr(account, "order_volume_ratio", None)
    if ratio is None:
        return shares
    vol = bar_get(bar, "vol")
    if vol is None or vol != vol:  # 无 vol 或 NaN 不限制
        return shares
    return min(shares, int(vol * ratio) * 100)


def apply_partial_sell(holding, sold: int) -> None:
    """部分卖出后按剩余比例缩减持仓股数与成本。"""
    remaining = holding.shares - sold
    holding.cost *= remaining / holding.shares
    holding.shares = remaining


# 可成交校验的原因代码，供调用点分支记日志
LIMIT_UNKNOWN = "LIMIT_UNKNOWN"  # 涨跌停无法判定（缺 pre_close / 未知板块）
INVALID_PRICE = "INVALID_PRICE"  # 成交价 None / NaN / 非正
LIMIT_UP = "LIMIT_UP"  # 买侧封涨停
LIMIT_DOWN = "LIMIT_DOWN"  # 卖侧封跌停


def check_tradable(side: str, price, up, down) -> str | None:
    """可成交校验：涨跌停可判定 + 价格合法 + 未封板（涨停不买 / 跌停不卖）。

    返回 None 表示可成交，否则返回原因代码；日志由调用点按原因各自输出。
    """
    if up is None or down is None:
        return LIMIT_UNKNOWN
    if not is_valid_price(price):
        return INVALID_PRICE
    if side == "BUY" and price >= up:
        return LIMIT_UP
    if side == "SELL" and price <= down:
        return LIMIT_DOWN
    return None


def _execute_trade(account, side: str, symbol: str, bar, shares: int,
                   fill_price: float, trigger: str, costs_fn, slip_fn,
                   slip_ticks: int | None = None) -> Trade:
    """买卖统一的成交结算：滑点（买 +n 档 / 卖 -n 档）+ 费用 + 现金出入账。

    slip_ticks 为 None 时用 account.slippage_ticks（条件单可传入独立档数）。
    """
    direction = 1 if side == "BUY" else -1
    ticks = account.slippage_ticks if slip_ticks is None else slip_ticks
    fill_price_slipped = slip_fn(fill_price, ticks, direction)
    raw_turnover = fill_price * shares
    slipped_turnover = fill_price_slipped * shares
    # 滑点金额统一记为不利方向的正数成本
    slippage_amount = direction * (slipped_turnover - raw_turnover)

    costs = costs_fn(side, slipped_turnover)
    commission = costs["commission"]
    stamp_tax = costs["stamp_tax"]
    transfer_fee = costs["transfer_fee"]
    if side == "BUY":
        # 买入净流出（成交额 + 费用，买侧印花税为 0）；cash += net_amount 买卖通用
        net_amount = -(slipped_turnover + commission + transfer_fee)
    else:
        net_amount = slipped_turnover - commission - stamp_tax - transfer_fee

    account.cash += net_amount

    return Trade(
        date=_date(bar),
        symbol=symbol,
        side=side,
        trigger=trigger,
        price=fill_price_slipped,
        shares=shares,
        turnover=raw_turnover,
        commission=commission,
        stamp_tax=stamp_tax,
        transfer_fee=transfer_fee,
        slippage_amount=slippage_amount,
        net_amount=net_amount,
        reason=trigger,
    )


def execute_sell(account, holding, bar, fill_price: float, trigger: str,
                 costs_fn, slip_fn, shares: int | None = None,
                 slip_ticks: int | None = None) -> Trade:
    if shares is None:
        shares = holding.shares
    return _execute_trade(account, "SELL", holding.symbol, bar, shares,
                          fill_price, trigger, costs_fn, slip_fn,
                          slip_ticks=slip_ticks)


def execute_buy(account, symbol: str, bar, shares: int, fill_price: float,
                trigger: str, costs_fn, slip_fn,
                slip_ticks: int | None = None) -> Trade:
    return _execute_trade(account, "BUY", symbol, bar, shares,
                          fill_price, trigger, costs_fn, slip_fn,
                          slip_ticks=slip_ticks)
