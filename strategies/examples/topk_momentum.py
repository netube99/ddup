"""
示例 1：因子打分轮动 — 进阶策略。

从 simple_rotation 基础之上增加了：
  - on_fills 钩子（感知成交 → 冷却期管理）
  - buy_weights 自定义买入金额分配
  - calc_conditions 中 holding_days 自适应调参
  - REQUIRED_FIELDS 列裁剪声明

如果你是第一次看示例，先从 simple_rotation 开始。
"""

import logging
from typing import Optional

from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

logger = logging.getLogger(__name__)


class TopKMomentum(Strategy):
    """每日对候选池按多因子合成得分排序，持有得分最高的 top_k 只。

    卖出不在 top_k 的持仓；新买入按得分加权分配资金（buy_weights）。
    条件单包含止损 + 止盈 + 移动止盈，止损幅度随持仓天数自适应调整。
    on_fills 钩子感知实际成交，对条件单止损退出的标的施加冷却期。
    """

    # 声明 select() 中命令式访问的额外列，确保引擎 preload 时不被裁剪
    REQUIRED_FIELDS: list[str] = []

    def on_start(self, provider, first_date: str, end_date: Optional[str] = None) -> None:
        """回测开始前初始化。

        StockFilter 和 ConditionBuilder 的配置全部来自 YAML——
        FILTER_RULES 和 config.conditions 由 loader 自动注入实例属性。
        """
        self._top_k = int(self.config.get("top_k", 5))
        self._cooldown_days = int(self.config.get("cooldown_days", 3))

        # 过滤器：将 YAML filter_rules 声明转换为可调用的截面过滤对象
        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )

        # 条件单构建器：stop_loss / take_profit / trailing 三种规则
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

        # 冷却期：记录因条件单退出的标的及其冷却截止日
        self._cooldown: dict[str, int] = {}

    def on_fills(self, trades, provider):
        """每日 select 之前由引擎调用，传入当日实际成交列表。

        这里感知条件单触发事件：
          - 条件单卖出的标的进入冷却期（避免立即买回）
          - STOP_LOSS 触发延长的冷却期（信号更负面）
          - 手动卖出 / 条件买入不进入冷却
        """
        for t in trades:
            if t.side == "SELL" and t.trigger in (
                "STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP"
            ):
                # STOP_LOSS 含义更强，冷却期翻倍
                cd = self._cooldown_days * 2 if t.trigger == "STOP_LOSS" else self._cooldown_days
                self._cooldown[t.symbol] = int(t.date) + cd

    def select(self, bars, account_snapshot, provider) -> dict:
        """每日核心决策。

        流程：
          1. 截面过滤（ST / 次新 / 亏损 / 低价 / 板块）
          2. 排除冷却期标的
          3. 按 FACTOR_SPECS 合成得分，降序排列
          4. 前 top_k 为目标持仓，不在其中的持仓卖出
          5. 买入按得分加权分配资金
        """
        if not bars:
            return {"buy": [], "sell": []}

        # ── 冷却期递减 ──
        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0
        expired = [s for s, d in self._cooldown.items() if d <= date_int]
        for s in expired:
            del self._cooldown[s]

        # ── 截面过滤 ──
        filtered = self._filter.filter(bars, date_str)

        # ── 因子打分 ──
        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # 排除冷却期标的
        score = score[~score.index.isin(self._cooldown)]

        # ── 选股 ──
        sorted_score = score.sort_values(ascending=False)
        target = set(sorted_score.head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        # ── 清理条件单状态（已平仓标的的 trailing high 锚点）──
        self._cond.prune(current)

        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # ── 买入权重：按得分比例分配 ──
        buy_weights = {}
        if buy_list:
            raw = score.loc[buy_list].clip(lower=0)
            total = raw.sum()
            if total > 0:
                for sym in buy_list:
                    buy_weights[sym] = float(raw[sym] / total * 0.9)  # 留 10% 现金
            else:
                eq = 0.9 / len(buy_list)
                for sym in buy_list:
                    buy_weights[sym] = eq

        return {
            "buy": buy_list,
            "sell": sell_list,
            "buy_weights": buy_weights if buy_weights else None,
        }

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """每个持仓每日的条件单计算。

        holding_days 自适应调参：
          - 前 3 天：止损收紧（保护新仓），不挂止盈
          - 第 4-30 天：标准止损
          - 30 天以上：止损放宽（给趋势充足空间），不挂止盈
        """
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        for c in conds:
            if c.get("type") == "STOP_LOSS":
                if holding_days <= 3:
                    # 新仓保护：买入后立即大跌应快速止损
                    c["price"] = entry_price * 0.97
                elif holding_days > 30:
                    # 长期持仓：趋势已经确立，放宽止损
                    c["price"] = entry_price * 0.85
            elif c.get("type") == "TAKE_PROFIT" and holding_days <= 3:
                # 新仓不给止盈，避免刚买就被小涨震出
                conds.remove(c)

        return conds
