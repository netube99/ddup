"""
示例 3：条件单猎手 — buy_conditions + 自定义 handler 深度展示。

展示的核心能力：
  - buy_conditions 条件买入（LIMIT_BUY 限价回踩 + BREAKOUT_BUY 突破追涨）
  - 自定义条件单 handler（DYNAMIC_STOP 波动率自适应止损）
  - 自定义条件买入 handler（VWAP_BUY 均价买入）
  - register_condition_handler / register_buy_condition_handler 进程级注册
  - calc_conditions 中组装多种条件单
  - 条件单独立滑点参数 condition_slippage_ticks
  - select 中策略层自行构造 buy_conditions 列表

关键概念：
  - 条件买入在 T 日声明、T+1 日盘中触发、单日有效
  - 买侧约束（涨停不买、停牌跳过、成交量 cap、max_positions 上限）自动生效
  - 条件买入与 buy/sell 名单不冲突（和 target_value 互斥）
  - 自定义 handler 必须在 on_start 中注册（进程级全局，不能是类级别）

用法：
  python scripts/run.py strategies/examples/condition_hunter.yaml --start 20240101 --end 20240630
"""
import logging
from typing import Optional

from btcore.filters import StockFilter
from btcore.match.conditions import (
    register_buy_condition_handler,
    register_condition_handler,
)
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 自定义条件单 handler — 进程级全局注册（必须在 on_start 中调用）
# ══════════════════════════════════════════════════════════════════════

def _dynamic_stop_handler(holding, cond, bar):
    """波动率自适应止损：波动率越高，止损越宽。

    止损价 = entry_price × (1 - max(3%, min(10%, vol_ratio × 2%)))
    其中 vol_ratio 由策略在 calc_conditions 中传入。

    返回 (executed: bool, fill_price: float, log_params: dict)
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
    """VWAP 均价买入：价格回落到日内均价估算值附近时成交。

    VWAP 估算 = （最高价 + 最低价 + 收盘价）/ 3
    只有 open 或 low 触达该价位才触发。
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

    主策略用 buy/sell 名单持有 top_k 标的，
    对紧随其后的备选标的挂 LIMIT_BUY（回踩）和 VWAP_BUY（均价）条件买单，
    对强势突破候选挂 BREAKOUT_BUY 条件买单。
    自定义 DYNAMIC_STOP 替代固定止损，波动率高时给更多空间。
    """

    REQUIRED_FIELDS: list[str] = []

    def on_start(self, provider, first_date: str, end_date: Optional[str] = None) -> None:
        # ── 注册自定义 handler（必须在 on_start 中，因为注册是进程级全局）──
        register_condition_handler("DYNAMIC_STOP", _dynamic_stop_handler)
        register_buy_condition_handler("VWAP_BUY", _vwap_buy_handler)

        self._top_k = int(self.config.get("top_k", 5))
        self._hunt_count = int(self.config.get("hunt_count", 3))  # 条件买入候选数
        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # ── 主仓：top_k ──
        sorted_score = score.sort_values(ascending=False)
        target = set(sorted_score.head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())
        self._cond.prune(current)

        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # ── 条件买入：top_k 之后的 hunt_count 只作为条件买单候选 ──
        # LIMIT_BUY: 在现价 98% 回踩时买入，小仓位试探
        # BREAKOUT_BUY: 在现价 102% 突破时追涨
        # VWAP_BUY: 日内均价附近买入
        buy_conditions = []
        total_value = account_snapshot.total_value
        hunt_size = total_value * 0.02  # 每只 2% 试探仓位

        candidates = sorted_score.iloc[self._top_k:self._top_k + self._hunt_count]
        for sym in candidates.index:
            if sym in sell_list:
                continue
            bar = filtered.get(sym, {})
            close = bar.get("close", 0) or 0
            if close <= 0:
                continue

            # 限价回踩买入
            buy_conditions.append({
                "symbol": sym, "type": "LIMIT_BUY",
                "price": round(close * 0.98, 2), "value": hunt_size,
            })
            # VWAP 均价买入
            buy_conditions.append({
                "symbol": sym, "type": "VWAP_BUY",
                "price": round(close * 0.99, 2), "value": hunt_size,
            })
            # 突破追涨（仅对动量最强的 1 只）
            if candidates.index.get_loc(sym) == 0:
                buy_conditions.append({
                    "symbol": sym, "type": "BREAKOUT_BUY",
                    "price": round(close * 1.02, 2), "value": hunt_size,
                })

        return {
            "buy": buy_list,
            "sell": sell_list,
            "buy_conditions": buy_conditions,
        }

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """组装多层条件单。

        标准条件单（来自 ConditionBuilder）：
          - TAKE_PROFIT：固定止盈
          - TRAILING_TP：移动止盈

        自定义条件单：
          - DYNAMIC_STOP：替换标准止损，用当日涨跌幅和换手率估算日内波动
        """
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        # 估算当日波动（用于自适应止损宽度）
        pct_chg = bar.get("pct_chg", 0) or 0
        turnover = bar.get("turnover_rate", 0) or 0
        est_vol = abs(pct_chg) * (1 + min(turnover, 0.10) * 10)

        conds.append({
            "symbol": symbol,
            "type": "DYNAMIC_STOP",
            "price": None,  # handler 自己算
            "vol_ratio": min(est_vol, 0.15),
        })

        # 移除标准止损（DYNAMIC_STOP 替代），但保留止盈和移动止盈
        conds = [c for c in conds if c.get("type") != "STOP_LOSS"]

        return conds
