"""
TrendGuard v2 — 月频价值轮动 + 日频趋势走坏离场。

v2 改进（基于交易数据分析）：
  - 趋势守卫激活延至 7 天（v1: 3d → 6笔净亏-1333，6d+ 全部盈利）
  - trend_min_hold 可配置
"""

import logging
from typing import Optional

from btcore.filters import StockFilter
from btcore.match.conditions import register_condition_handler
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

logger = logging.getLogger(__name__)


def _trend_break_handler(holding, cond, bar):
    """趋势走坏离场：多信号确认趋势恶化。

    cond 需包含:
      threshold: 需要同时触发的信号数 (默认 2)

    信号列表：
      1. MACD 死叉: macd_golden == 0 (DIF < DEA)
      2. BBI 跌破: close_vs_bbi < 0
      3. EMA 排列转空: ema_bullish < 3
      4. PDI < MDI: pdi_mdi < 0
    """
    threshold = cond.get("threshold", 2)
    signals = 0
    details = {}

    # 信号 1: MACD 死叉
    mg = bar.get("macd_golden")
    if mg is not None and float(mg) == 0:
        signals += 1
        details["macd_dead"] = True

    # 信号 2: BBI 跌破
    cvb = bar.get("close_vs_bbi")
    if cvb is not None and float(cvb) < 0:
        signals += 1
        details["below_bbi"] = True

    # 信号 3: EMA 排列转弱
    eb = bar.get("ema_bullish")
    if eb is not None and float(eb) < 3:
        signals += 1
        details["ema_weak"] = True

    # 信号 4: DMI 空头主导
    pm = bar.get("pdi_mdi")
    if pm is not None and float(pm) < 0:
        signals += 1
        details["dmi_bear"] = True

    if signals >= threshold:
        details["trend_signals"] = signals
        if bar.get("open"):
            return (True, float(bar["open"]), details)
        if bar.get("close"):
            return (True, float(bar["close"]), details)
        return (False, 0.0, details)

    return (False, 0.0, {})


class TrendGuard(Strategy):
    """月频价值轮动 + 日频趋势走坏离场。"""

    REQUIRED_FIELDS: list[str] = []
    CONDITION_FACTORS = {"macd_golden", "close_vs_bbi", "ema_bullish", "pdi_mdi"}

    def on_start(self, provider, first_date: str, end_date: Optional[str] = None) -> None:
        self._top_k = int(self.config.get("top_k", 5))
        self._cooldown_days = int(self.config.get("cooldown_days", 15))
        self._trend_guard_cooldown = int(self.config.get("trend_guard_cooldown", 30))
        self._rebalance_interval = int(self.config.get("rebalance_interval", 22))
        self._trend_break_threshold = int(self.config.get("trend_break_threshold", 2))

        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))
        self._cooldown: dict[str, int] = {}
        self._days_since_rebalance: int = 999

        register_condition_handler("TREND_BREAK", _trend_break_handler)

    def on_fills(self, trades, provider):
        for t in trades:
            if t.side == "SELL":
                if t.trigger == "TREND_BREAK":
                    self._cooldown[t.symbol] = int(t.date) + self._trend_guard_cooldown
                elif t.trigger in ("STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP"):
                    cd = (
                        self._cooldown_days * 2
                        if t.trigger == "STOP_LOSS"
                        else self._cooldown_days
                    )
                    self._cooldown[t.symbol] = int(t.date) + cd

    def on_tick(self, bars, snapshot, provider) -> None:
        """每日状态维护：冷却期递减 + trailing 锚点清理。"""
        if not bars:
            return

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0
        expired = [s for s, d in self._cooldown.items() if d <= date_int]
        for s in expired:
            del self._cooldown[s]

        self._cond.prune(set(snapshot.holdings.keys()))

    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        self._days_since_rebalance += 1
        if self._days_since_rebalance < self._rebalance_interval:
            return {"buy": [], "sell": []}
        self._days_since_rebalance = 0

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        score = score[~score.index.isin(self._cooldown)]

        sorted_score = score.sort_values(ascending=False)
        target = set(sorted_score.head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # P2-2: 现金预算保护
        max_pos = max(int(self.config.get("max_positions", self._top_k)), 1)
        per_pos = account_snapshot.total_value / max_pos
        affordable = max(1, int(account_snapshot.cash / (per_pos * 0.95)))
        buy_list = buy_list[:affordable]

        return {"buy": buy_list, "sell": sell_list}

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        # 内置条件单：宽止盈 + 移动止盈（无固定止损）
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        # 移除新仓止盈限制
        conds = [
            c for c in conds if not (c.get("type") == "TAKE_PROFIT" and holding_days <= 3)
        ]

        # 趋势走坏检测放在最高优先级（在止盈之前）
        # 持有 > 3 天后才启用（数据验证：3d 激活优于 7d）
        if holding_days > 3:
            conds.insert(0, {
                "type": "TREND_BREAK",
                "price": entry_price * 0.85,  # fallback 价格避免分红调整崩溃，handler 内部覆盖
                "threshold": self._trend_break_threshold,
            })

        return conds
