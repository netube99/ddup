"""
示例 1: rolling_ranker — 进阶因子轮动。

在 bare_bones 的骨架之上新增：
  - on_fills   — 成交感知 → 条件单卖出后的冷却期
  - on_tick    — 每日冷却期递减 + ConditionBuilder 状态修剪
  - buy_weights — 按因子得分比例分配买入资金
  - calc_conditions holding_days 自适应 — 新仓紧止损，老仓放宽
  - REQUIRED_FIELDS — 声明策略命令式访问的额外列

下一级 target_allocator 展示 target_value / 时间门控自管理。
"""

from btcore.strategy import Strategy
from btcore.strategy_tools import bars_to_df, eval_factor_specs


class RollingRanker(Strategy):
    """每日按因子得分排序，持有得分最高的 top_k 只。

    核心路径：过滤 → 打分 → 排除冷却期 → 选 top_k → 加权买 / 清仓卖。
    """

    # ── REQUIRED_FIELDS ──────────────────────────────────────────────────
    # 声明 select() 中命令式访问的 bar 列，确保引擎 preload 列裁剪时保留。
    # 基础 OHLCV 列引擎永不裁剪；因子列由 FACTOR_SPECS 自动覆盖，
    # 无需在此声明。这里声明的是 bar["non_factor_column"] 这类访问。
    REQUIRED_FIELDS: list[str] = []

    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        super().on_start(provider, first_date, end_date)
        self._top_k = int(self.config.get("top_k", 5))
        self._cooldown_days = int(self.config.get("cooldown_days", 3))

        # 冷却期 map：symbol → 冷却截止日 (YYYYMMDD int)
        self._cooldown: dict[str, int] = {}

    # ── on_fills: 成交感知 ────────────────────────────────────────────────
    def on_fills(self, trades, provider):
        """引擎在 select 之前调用，传入当日已撮合成交列表。

        感知条件单触发事件，对退出的标的施加冷却期：
          - STOP_LOSS / TAKE_PROFIT / TRAILING_TP → 进入冷却期
          - MANUAL 卖出 / TARGET / 条件买入 → 不冷却
        """
        for t in trades:
            # trigger 标识成交来源：MANUAL / TARGET / STOP_LOSS / TAKE_PROFIT / ...
            if t.side == "SELL" and t.trigger in (
                "STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP"
            ):
                self._cooldown[t.symbol] = int(t.date) + self._cooldown_days

    # ── on_tick: 每日状态维护 ─────────────────────────────────────────────
    def on_tick(self, bars, snapshot, provider) -> None:
        """每日运行——策略即使在 select 中自行管理调仓节奏，on_tick 也不受影响。

        维护项：
          1. 冷却期到期清理
          2. ConditionBuilder 修剪已平仓标的的 trailing 锚点
        """
        if not bars:
            return

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0

        # 冷却期到期 → 允许重新买入
        expired = [s for s, d in self._cooldown.items() if d <= date_int]
        for s in expired:
            del self._cooldown[s]

        # 清理已平仓标的的 trailing high 锚点（基类默认 on_tick 负责）
        super().on_tick(bars, snapshot, provider)

    # ── select: 每日买卖决策 ──────────────────────────────────────────────
    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")

        # 截面过滤
        filtered = self.filter_bars(bars, date_str)

        # 因子打分
        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # 排除冷却期标的（条件单刚卖出的不立即买回）
        score = score[~score.index.isin(self._cooldown)]

        # 选股：前 top_k 是目标
        sorted_score = score.sort_values(ascending=False)
        target = set(sorted_score.head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # ── buy_weights: 按因子得分比例分配资金 ─────────────────────────
        # 引擎等权分配是 total_value / max_positions。
        # 提供 buy_weights 可让高分标的获得更多资金。所有权重和 ≤ 1，
        # 剩余现金保留在账户中。
        buy_weights = None
        if buy_list:
            raw = score.loc[buy_list].clip(lower=0)
            total = raw.sum()
            if total > 0:
                # 留 10% 现金缓冲
                buy_weights = {sym: float(raw[sym] / total * 0.9) for sym in buy_list}

        return {"buy": buy_list, "sell": sell_list, "buy_weights": buy_weights}

    # ── calc_conditions: 持仓条件单 ───────────────────────────────────────
    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """每个持仓每日的条件单，支持 holding_days 自适应调参。

        holding_days 自适应区间：
          ≤3 天   — 止损收紧至 3%（新仓保护），不挂止盈
          4-30 天 — 标准止损（YAML 声明的 6%）
          >30 天  — 止损放宽至 15%（给趋势充足空间），不挂止盈
        """
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        for c in conds:
            if c.get("type") == "STOP_LOSS":
                if holding_days <= 3:
                    c["price"] = entry_price * 0.97      # 新仓紧止损
                elif holding_days > 30:
                    c["price"] = entry_price * 0.85      # 老仓放宽
            elif c.get("type") == "TAKE_PROFIT" and holding_days <= 3:
                conds.remove(c)  # 新仓不挂止盈，避免小涨震出

        return conds
