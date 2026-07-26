"""
示例 0：裸因子轮动 — 最简入门。

只展示最基本的：
  - 因子打分选股
  - buy/sell 名单调仓
  - ConditionBuilder 条件单（一行委托）

没有 on_fills、没有 buy_weights、没有 schedule、没有自定义 handler。
就是：每天排名 → 持有前 N 只 → 卖了换新的。

如果这个能跑通，再看 topk_momentum（加了 on_fills + buy_weights + 动态调参）。
"""
from typing import Optional

from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs


class SimpleRotation(Strategy):
    """每天按因子得分排序，持有得分最高的 top_k 只。"""

    def on_start(self, provider, first_date: str, end_date: Optional[str] = None) -> None:
        self._top_k = int(self.config.get("top_k", 5))
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

        target = set(score.sort_values(ascending=False).head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())
        self._cond.prune(current)

        return {
            "buy": sorted(target - current),
            "sell": sorted(current - target),
        }

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        return self._cond.calc(symbol, entry_price, bar, holding_days)
