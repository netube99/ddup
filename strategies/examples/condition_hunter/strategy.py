"""
示例 3: condition_hunter — 条件单系统完整展示。

展示 buy_conditions / 自定义 handler / register_* / on_tick buy_conditions。

核心路径：过滤 → 打分 → 选 top_k 作为主仓 buy/sell
  → 备选标的挂条件买单（LIMIT_BUY 回踩 / BREAKOUT_BUY 突破 / VWAP_BUY 均价）
  → 自定义 DYNAMIC_STOP 替代固定止损

下一级 multi_model 展示多模型投票与状态机。
"""

from btcore.filters import StockFilter
from btcore.match.conditions import (
    register_buy_condition_handler,
    register_condition_handler,
)
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

# ══════════════════════════════════════════════════════════════════════════
# 自定义条件单 handler — 模块级函数，在 on_start 中注册（进程级全局）
# ══════════════════════════════════════════════════════════════════════════

def _dynamic_stop_handler(holding, cond, bar):
    """波动率自适应止损。

    止损价 = entry_price × (1 - max(3%, min(10%, vol_ratio × 2%)))
    vol_ratio 由策略在 calc_conditions 中根据当日波动估算后传入。
    open 触达 → fill at open；否则 low 触达 → fill at price。

    返回 (executed: bool, fill_price: float, log_params: dict)。
    """
    vol_ratio = cond.get("vol_ratio", 0.05)
    width = max(0.03, min(0.10, vol_ratio * 0.02))
    stop_price = holding.entry_price * (1 - width)

    if bar["open"] <= stop_price:
        return (True, bar["open"], {"width": round(width, 4), "trigger": "open"})
    if bar["low"] <= stop_price:
        return (True, stop_price, {"width": round(width, 4), "trigger": "intraday"})
    return (False, 0.0, {})


def _vwap_buy_handler(order, bar):
    """VWAP 均价买入：价格回落到日内均价估算值时成交。

    VWAP 估算 = (high + low + close) / 3。
    open 或 low 触达该价位才触发。

    返回 (executed: bool, fill_price: float, log_params: dict)。
    """
    h, lo, c = bar.get("high"), bar.get("low"), bar.get("close")
    if not all([h, lo, c]):
        return (False, 0.0, {})

    vwap_est = (h + lo + c) / 3
    if bar["open"] <= vwap_est:
        return (True, bar["open"], {"vwap": round(vwap_est, 2)})
    if bar["low"] <= vwap_est:
        return (True, vwap_est, {"vwap": round(vwap_est, 2)})
    return (False, 0.0, {})


class ConditionHunter(Strategy):
    """标准因子轮动 + 条件买入增强 + 自定义条件单。

    主策略用 buy/sell 名单持有 top_k 标的，同时对备选标的下条件买单
    ——这些买单在非调仓日也可通过 on_tick 提交。
    """

    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        # ── 注册自定义 handler（必须在此调用，进程级全局）──
        register_condition_handler("DYNAMIC_STOP", _dynamic_stop_handler)
        register_buy_condition_handler("VWAP_BUY", _vwap_buy_handler)

        self._top_k = int(self.config.get("top_k", 5))
        self._hunt_count = int(self.config.get("hunt_count", 3))
        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

        # 记录上一日的 top_k 之外候选（供 on_tick 突破买入用）
        self._watchlist: dict[str, float] = {}

    # ── on_tick: 非调仓日条件买单 ────────────────────────────────────────
    def on_tick(self, bars, snapshot, provider) -> dict | None:
        """每日维护 + 突破信号检测。

        如果当前有候选标的的价格突破前日 close 的 102%，提交 BREAKOUT_BUY。
        on_tick 每日运行，不受 select 中调仓判断影响。
        返回的 buy_conditions 会合并到 select() 返回的 buy_conditions 中。
        """
        # 修剪条件单状态
        self._cond.prune(set(snapshot.holdings.keys()))

        # 检测突破，提交非调仓日条件买单
        orders = []
        for sym, ref_close in list(self._watchlist.items()):
            bar = bars.get(sym)
            if not bar:
                continue
            if bar.get("high", 0) >= ref_close * 1.03:  # 突破 3%
                orders.append({
                    "symbol": sym,
                    "type": "BREAKOUT_BUY",
                    "price": round(ref_close * 1.03, 2),
                    "value": snapshot.total_value * 0.02,
                })
                del self._watchlist[sym]  # 单日只挂一次

        if orders:
            return {"buy_conditions": orders}
        return None

    # ── select: 主仓 + 条件买单 ───────────────────────────────────────────
    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        sorted_score = score.sort_values(ascending=False)
        target = set(sorted_score.head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # ── 条件买入：top_k 之后的 hunt_count 只作为条件买单候选 ─────────
        # LIMIT_BUY: 现价 98% 回踩买入（限价低吸）
        # BREAKOUT_BUY: 现价 102% 突破追涨
        # VWAP_BUY: 日内均价附近买入
        buy_conditions = []
        hunt_size = account_snapshot.total_value * 0.02  # 每只 2% 试探仓位

        candidates = sorted_score.iloc[self._top_k:self._top_k + self._hunt_count]
        new_watchlist = {}
        for sym in candidates.index:
            if sym in sell_list:
                continue
            bar = filtered.get(sym, {})
            close = bar.get("close", 0) or 0
            if close <= 0:
                continue
            new_watchlist[sym] = close

            buy_conditions.append({
                "symbol": sym, "type": "LIMIT_BUY",
                "price": round(close * 0.98, 2), "value": hunt_size,
            })
            buy_conditions.append({
                "symbol": sym, "type": "VWAP_BUY",
                "price": round(close * 0.99, 2), "value": hunt_size,
            })
            # 仅对候选第 1 只挂突破买单
            if candidates.index.get_loc(sym) == 0:
                buy_conditions.append({
                    "symbol": sym, "type": "BREAKOUT_BUY",
                    "price": round(close * 1.02, 2), "value": hunt_size,
                })

        self._watchlist = new_watchlist

        return {"buy": buy_list, "sell": sell_list, "buy_conditions": buy_conditions}

    # ── calc_conditions: 多类型条件单组装 ────────────────────────────────
    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """组装条件单列表。

        标准（ConditionBuilder 翻译）：
          TAKE_PROFIT / TRAILING_TP

        自定义：
          DYNAMIC_STOP — 用当日涨跌幅和换手率估算波动，决定止损宽度，
                         替换标准 STOP_LOSS。

        引擎按列表顺序评估，首条触发生效即 break。
        """
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        # 估算当日波动
        pct_chg = bar.get("pct_chg", 0) or 0
        turnover = bar.get("turnover_rate", 0) or 0
        est_vol = abs(pct_chg) * (1 + min(turnover, 0.10) * 10)

        conds.append({
            "symbol": symbol,
            "type": "DYNAMIC_STOP",
            "price": None,  # handler 自己算止损价
            "vol_ratio": min(est_vol, 0.15),
        })

        # 移除标准 STOP_LOSS——DYNAMIC_STOP 替代
        conds = [c for c in conds if c.get("type") != "STOP_LOSS"]
        return conds
