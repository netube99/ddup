"""
示例 2: target_allocator — 目标仓位精确管理。

展示 target_value / sell_shares / 时间门控自管理 / risk_rules / materialize_only。

核心路径：过滤 → 打分 → 选 top_k → 按得分比例分配 target_value
  → 不在 top_k 的持仓 target=0（清仓）
  → 近边缘持仓 sell_shares 减半保留
  → 引擎自动计算买卖差额（trigger="TARGET"）

下一级 condition_hunter 展示条件单系统的全部入口。
"""

from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs


class TargetAllocator(Strategy):
    """按多因子得分比例分配目标市值，时间门控调仓。

    select() 每日运行，策略代码自行管理调仓节奏。
    target_value 返回格式下引擎自动计算买卖差额：
      - 目标市值 > 当前市值 → 加仓（trigger="TARGET"）
      - 目标市值 < 当前市值 → 减持
      - 目标市值 = 0 → 清仓
      - 未出现在 dict 中的持仓 → 不动

    配合 risk_rules：
      - max_position_pct 限制单票买入上限
      - max_industry_pct 行业总暴露闸门
      - max_drawdown 触发熔断后清仓 + 冷却
    """

    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        self._top_k = int(self.config.get("top_k", 8))
        self._rebalance_interval = int(self.config.get("rebalance_interval", 5))
        self._last_rebalance = 0
        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": [], "target_value": {}}

        # ── 熔断感知：冷却期内暂停所有买入 ───────────────────────────
        if account_snapshot.risk_active:
            return {"buy": [], "sell": [], "target_value": {}}

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0

        # ── 时间门控：非调仓日不操作 ───────────────────────────────────
        # select 每日运行，策略代码自行判断是否调仓。
        is_rebalance_day = (date_int - self._last_rebalance) >= self._rebalance_interval
        if not is_rebalance_day:
            return {"buy": [], "sell": [], "target_value": {}}

        self._last_rebalance = date_int

        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        factor_df, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # ── 选股：得分前 top_k ──
        sorted_score = score.sort_values(ascending=False)
        top_symbols = sorted_score.head(self._top_k)

        current = set(account_snapshot.holdings.keys())

        # ── 构建 target_value ───────────────────────────────────────────
        # total_value 来自 snapshot（现金 + 持仓市值），是当日结算后的总资产。
        total_value = account_snapshot.total_value
        allocable = total_value * 0.95  # 留 5% 现金缓冲

        target_value: dict[str, float] = {}

        # 不在 top_k 的持仓：target = 0（清仓）
        for sym in current:
            if sym not in top_symbols.index:
                target_value[sym] = 0.0

        # 在 top_k 的：按因子得分比例分配
        raw_w = top_symbols.clip(lower=0)
        w_sum = raw_w.sum()
        if w_sum > 0:
            for sym in top_symbols.index:
                target_value[sym] = float(allocable * raw_w[sym] / w_sum)

        # ── sell_shares：近边缘持仓减半保留 ─────────────────────────────
        # 排名在 top_k × 1.5 范围内的持仓不减到 0，而是保留一半仓位。
        # 这样做的目的是降低换手率——这些标的"没那么差"，不必全清。
        near_top = set(sorted_score.head(int(self._top_k * 1.5)).index)
        for sym in list(target_value):
            if target_value[sym] == 0.0 and sym in near_top:
                h = account_snapshot.holdings.get(sym)
                if h and h.shares >= 200:
                    target_value[sym] = h.last_price * h.shares * 0.5

        # ── materialize_only 因子使用 ───────────────────────────────────
        # pct_above_ma20 列在 factor_df 中可用（物化了），但不在 score 中
        # （materialize_only=true 跳过了得分合成）。可在 calc_conditions
        # 中据此决定是否收紧止损。此处仅演示列存在性。
        _ = factor_df  # noqa: F841

        return {"buy": [], "sell": [], "target_value": target_value}

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """每个持仓的条件单。"""
        return self._cond.calc(symbol, entry_price, bar, holding_days)
