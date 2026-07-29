"""
示例 2：目标仓位调仓 — target_value 全功能。

展示的核心能力：
  - select 返回 target_value 做精确仓位管理（而非 buy/sell 名单）
  - risk_rules 组合风控（回撤熔断 + 单票上限）
  - sell_shares 部分减仓（持仓不在 top_k 但仍有价值的做减半而非清仓）
  - buy_weights 自定义买入金额分配
  - schedule 调仓调度（weekly/monthly）
  - on_start 中的策略参数动态解析
  - ConditionBuilder 的 prune 方法（清理已平仓标的的 trailing 状态）

target_value 与 buy/sell 名单的区别：
  - buy/sell: 等权买入 top_k，不在 top_k 的清仓
  - target_value: 按得分比例分配市值，超配减持、低配加仓
  - 两者同日不可混用（引擎自动校验）

用法：
  python scripts/run.py strategies/examples/target_allocator/config.yaml \
      --start 20240101 --end 20240630
"""
import logging
from typing import Optional

from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

logger = logging.getLogger(__name__)


class TargetAllocator(Strategy):
    """按多因子得分比例分配目标市值，每周调仓。

    target_value 形式下引擎自动计算买卖差额：
      - 目标市值 > 当前市值 → 加仓（trigger="TARGET"）
      - 目标市值 < 当前市值 → 减持
      - 目标市值 = 0 → 清仓
      - 未出现在 target_value 中的持仓不动

    配合 risk_rules 时：
      - max_position_pct 限制单票买入上限
      - max_drawdown 触发熔断后清仓 + 冷却
    """

    REQUIRED_FIELDS: list[str] = []

    def on_start(self, provider, first_date: str, end_date: Optional[str] = None) -> None:
        self._top_k = int(self.config.get("top_k", 8))
        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

        # 策略可以在这里做一次性的计算（如预加载历史数据、初始化模型参数等）
        logger.info("TargetAllocator: top_k=%d, max_positions=%d",
                    self._top_k, self.config.get("max_positions", 20))

    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": [], "target_value": {}}

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # ── 选股：得分前 top_k ──
        sorted_score = score.sort_values(ascending=False)
        top_symbols = sorted_score.head(self._top_k)

        current = set(account_snapshot.holdings.keys())
        self._cond.prune(current)

        # ── 构建 target_value ──
        total_value = account_snapshot.total_value
        # 只分配 95% 资金（留现金缓冲应付费用）
        allocable = total_value * 0.95

        target_value: dict[str, float] = {}

        # 不在 top_k 的持仓：target = 0（清仓）
        for sym in current:
            if sym not in top_symbols.index:
                target_value[sym] = 0.0

        # 在 top_k 的：按得分比例分配
        raw_w = top_symbols.clip(lower=0)
        w_sum = raw_w.sum()
        if w_sum > 0:
            for sym in top_symbols.index:
                target_value[sym] = float(allocable * raw_w[sym] / w_sum)

        # ── sell_shares：对 top_k*1.5 内的减半而非清仓 ──
        # 在 top_k 之外但排名尚可的持仓，保留一半仓位
        near_top = set(sorted_score.head(int(self._top_k * 1.5)).index)
        for sym in list(target_value):
            if target_value[sym] == 0.0 and sym in near_top:
                h = account_snapshot.holdings.get(sym)
                if h and h.shares >= 200:
                    # 保留现有仓位的 50%
                    target_value[sym] = h.last_price * h.shares * 0.5

        return {"buy": [], "sell": [], "target_value": target_value}

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        return self._cond.calc(symbol, entry_price, bar, holding_days)
