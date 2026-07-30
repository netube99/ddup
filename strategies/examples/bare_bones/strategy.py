"""
示例 0: bare_bones — 最小可运行策略骨架。

三板斧：
  1. StockFilter.filter()   — 过滤
  2. eval_factor_specs()    — 打分
  3. ConditionBuilder.calc() — 条件单

没有任何进阶能力——看懂这个就能写策略。下一级 rolling_ranker 加入
on_fills / on_tick / buy_weights / holding_days 自适应。
"""

from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs


class BareBones(Strategy):
    """每天按因子得分排序，持有得分最高的 top_k 只。"""

    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        # ── 三板斧第一斧：StockFilter ──
        # 在 first_date 做一次全量预筛选以加速后续逐日过滤。
        self._top_k = int(self.config.get("top_k", 5))
        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        # ── 三板斧第三斧：ConditionBuilder ──
        # 把 YAML conditions 节翻译为条件单逻辑。
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

    def select(self, bars, account_snapshot, provider) -> dict:
        """每日买卖决策。

        引擎传入的 bars 是当日截面 dict-of-dicts，键为 symbol。
        account_snapshot.holdings 是当前持仓的深拷贝。
        返回 {"buy": [...], "sell": [...]}，引擎次日撮合。
        """
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        # ── 第一斧：过滤 ──
        filtered = self._filter.filter(bars, date_str)

        # ── 第二斧：打分 ──
        # bars_to_df 把 dict-of-dicts 转为 symbol-indexed DataFrame。
        # eval_factor_specs 读取引擎物化好的因子列，合成 0~1 得分。
        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # 前 top_k 只是目标持仓，current 是现有持仓
        target = set(score.sort_values(ascending=False).head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        return {
            "buy": sorted(target - current),   # 不在目标中的 → 买入
            "sell": sorted(current - target),  # 不在目标中的 → 清仓
        }

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """每个持仓每日生成条件单列表。

        ── 第三斧：委托 ConditionBuilder ──
        把 YAML 声明的 stop_loss_pct 翻译为 STOP_LOSS 条件单 dict。
        引擎按列表顺序评估，首条触发生效即 break。
        """
        return self._cond.calc(symbol, entry_price, bar, holding_days)
