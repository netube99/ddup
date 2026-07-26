"""策略编写工具 — 用户 select() / calc_conditions() 的可选机制。

只是机制，不含任何买卖决策：截面数据整理、按 FACTOR_SPECS 求值并合成
得分、声明式条件单构建、调仓调度包装。排几名、买几只、何时调仓，
由用户策略自己决定。

schedule YAML 声明：

    schedule:
      frequency: weekly    # daily(默认)|weekly|monthly
      weekday: 1           # weekly: 每周第 N 个交易日（可负，-1=最后）
      monthday: 1          # monthly: 每月第 N 个交易日（可负）

分组语义：weekly 按 ISO (isoyear, isoweek) 分组，monthly 按 (year, month)
分组；N 为 1 起的组内第 N 个交易日，负数从组尾倒数（-1=组内最后交易日）。
N 超出组内天数时该组不调仓。
"""

import logging
from datetime import datetime

import pandas as pd

from btcore.types import bar_get

logger = logging.getLogger(__name__)


def bars_to_df(bars: dict) -> pd.DataFrame:
    """当日截面 dict-of-dicts → symbol 索引的 DataFrame，供 eval_factor_specs 使用。"""
    if not bars:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(bars, orient="index")


def eval_factor_specs(
    df: pd.DataFrame,
    factor_specs: list[dict],
) -> tuple[pd.DataFrame, pd.Series]:
    """按 FACTOR_SPECS 读物化因子列并合成加权得分。

    每条 spec: {name, weight=1.0, ascending=False}；因子值由引擎在
    preload 时物化为 df 的列（找不到列说明 FACTOR_NODES 未挂接）。
    各因子先转截面 percentile rank（ascending=True 时值小者得分高），
    再按 weight 加权平均为 score（∈ [0,1]，越大越优）。

    Returns:
        (factor_df, score): factor_df 每列一个因子值；score 为合成得分 Series，
        索引均为 symbol。factor_specs 为空时 score 为全 1.0。
    """
    factor_df = pd.DataFrame(index=df.index)
    score = pd.Series(0.0, index=df.index)
    total_weight = 0.0

    for spec in factor_specs or []:
        name = spec["name"]
        if name not in df.columns:
            raise ValueError(
                f"因子列 {name!r} 不在截面数据里——引擎未物化"
                "（策略缺少 FACTOR_NODES？请经 strategy_loader 加载）"
            )
        values = df[name]
        factor_df[name] = values
        weight = float(spec.get("weight", 1.0))
        pct_rank = values.rank(pct=True, ascending=not spec.get("ascending", False))
        score = score + pct_rank.fillna(0.0) * weight
        total_weight += weight

    if total_weight > 0:
        score = score / total_weight
    else:
        score = pd.Series(1.0, index=df.index)
    return factor_df, score


class ConditionBuilder:
    """由声明式规则生成条件单，并跟踪移动止盈所需的最高价状态。

    策略层工具：把 YAML 里声明的 conditions 规则翻译成引擎的条件单 dict。
    用户策略在 calc_conditions 里委托给本类；规则全空时返回空列表（不使用条件单）。

    支持的规则（值均为比例，∈ (0,1)）：
      stop_loss_pct   → STOP_LOSS    价格 = 成本价 * (1 - pct)
      take_profit_pct → TAKE_PROFIT  价格 = 成本价 * (1 + pct)
      trailing_pct    → TRAILING_TP  价格 = 持仓期间最高价 * (1 - pct)，最高价由本类逐日跟踪
    """

    def __init__(self, rules: dict):
        self._rules = rules or {}
        self._high: dict[str, float] = {}

    def calc(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """与 Strategy.calc_conditions 同签名，返回条件单 dict 列表。"""
        rules = self._rules
        if not rules:
            return []

        conds = []
        if "stop_loss_pct" in rules:
            conds.append({
                "type": "STOP_LOSS",
                "price": entry_price * (1 - rules["stop_loss_pct"]),
            })
        if "take_profit_pct" in rules:
            conds.append({
                "type": "TAKE_PROFIT",
                "price": entry_price * (1 + rules["take_profit_pct"]),
            })
        if "trailing_pct" in rules:
            close = bar_get(bar, "close", entry_price) or entry_price
            high = max(self._high.get(symbol, entry_price), close)
            self._high[symbol] = high
            conds.append({
                "type": "TRAILING_TP",
                "price": high * (1 - rules["trailing_pct"]),
            })
        return conds

    def prune(self, live_symbols) -> None:
        """清理已平仓标的的 trailing 状态。

        简便用法：在 select() 里以当前持仓调用本方法（基于持仓 diff）。
        精确用法：策略实现 on_fills hook，按 trigger 感知条件单平仓时点与价格。
        """
        live = set(live_symbols)
        for symbol in list(self._high):
            if symbol not in live:
                del self._high[symbol]


_KNOWN_KEYS = {"frequency", "weekday", "monthday"}
_FREQUENCIES = {"daily", "weekly", "monthly"}


def parse_schedule(raw: dict) -> dict:
    """校验并规范化 YAML schedule 键；未知键/值立即 ValueError。"""
    if not isinstance(raw, dict):
        raise ValueError("schedule 必须是 dict")
    for key in raw:
        if key not in _KNOWN_KEYS:
            raise ValueError(
                f"未知 schedule 键 {key!r}，支持: {sorted(_KNOWN_KEYS)}"
            )
    frequency = raw.get("frequency", "daily")
    if frequency not in _FREQUENCIES:
        raise ValueError(
            f"未知 schedule.frequency {frequency!r}，支持: {sorted(_FREQUENCIES)}"
        )
    rule = {"frequency": frequency}
    if frequency == "weekly":
        weekday = int(raw.get("weekday", 1))
        if weekday == 0:
            raise ValueError("schedule.weekday 从 1 起（可负），不能为 0")
        rule["weekday"] = weekday
    elif frequency == "monthly":
        monthday = int(raw.get("monthday", 1))
        if monthday == 0:
            raise ValueError("schedule.monthday 从 1 起（可负），不能为 0")
        rule["monthday"] = monthday
    return rule


def _rebalance_dates(calendar: list[str], rule: dict) -> set[str]:
    """按规则从交易日历算出调仓日集合。"""
    frequency = rule["frequency"]
    if frequency == "daily":
        return set(calendar)

    groups: dict[tuple, list[str]] = {}
    for day in calendar:
        dt = datetime.strptime(day, "%Y%m%d").date()
        if frequency == "weekly":
            key = dt.isocalendar()[:2]
        else:
            key = (dt.year, dt.month)
        groups.setdefault(key, []).append(day)

    n = rule["weekday"] if frequency == "weekly" else rule["monthday"]
    result = set()
    for days in groups.values():
        idx = n - 1 if n > 0 else n
        if -len(days) <= idx < len(days):
            result.add(days[idx])
    return result


def wrap_strategy(strategy, rule: dict):
    """实例级包装策略：只在调仓日透传 select，其余日期返回空名单。

    on_start 末尾用 provider.get_calendar(first_date, end_date) 预计算
    调仓日集合；end_date 为 None 时不包装（每日透传）。
    包装方式为实例属性遮蔽（on_start/select），与 loader 的实例注入风格一致；
    非调仓日 select 返回空买卖名单，calc_conditions 不受影响（条件单仍每日生成）。
    """
    orig_on_start = strategy.on_start
    orig_select = strategy.select

    def on_start(provider, first_date: str, end_date: str | None = None) -> None:
        orig_on_start(provider, first_date, end_date=end_date)
        if end_date is None:
            strategy._schedule_dates = None
            return
        calendar = provider.get_calendar(first_date, end_date)
        strategy._schedule_dates = _rebalance_dates(calendar, rule)
        logger.info("schedule %s: %d 个调仓日", rule, len(strategy._schedule_dates))

    def select(bars, account_snapshot, provider) -> dict:
        dates = getattr(strategy, "_schedule_dates", None)
        if dates is None:
            return orig_select(bars, account_snapshot, provider)
        if not bars:
            return {"buy": [], "sell": []}
        bar = next(iter(bars.values()))
        date_str = (
            bar.get("trade_date", "")
            if isinstance(bar, dict) else getattr(bar, "trade_date", "")
        )
        if date_str not in dates:
            return {"buy": [], "sell": []}
        return orig_select(bars, account_snapshot, provider)

    strategy.on_start = on_start
    strategy.select = select
    return strategy
