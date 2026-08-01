"""
示例 5: self_managed_rank — 排名阈值 + 逐仓独立管理。

展示能力：
  - 策略代码自管理换手：每只持仓独立判断，无固定调仓日
  - 卖出条件：排名 > top_k × sell_rank_mult 且持有 ≥ min_hold_days
  - 买入条件：有空位就从排名前列补入
  - on_fills 记录入场日期 / on_tick 清理跟踪状态
  - provider.get_historical_bars() 历史回溯查

模式：排名阈值
  - 换手频率由排名变化速度自然决定
  - 与 self_managed_time（时间门控模式）形成两种自管理方案
"""

from btcore.strategy import Strategy
from btcore.strategy_tools import bars_to_df, eval_factor_specs


class SelfManagedRank(Strategy):
    """排名阈值换手：每只持仓独立判断，无固定调仓日。"""

    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        super().on_start(provider, first_date, end_date)
        self._top_k = int(self.config.get("top_k", 5))
        self._min_hold_days = int(self.config.get("min_hold_days", 15))
        self._sell_rank_mult = float(self.config.get("sell_rank_mult", 2.0))

        # 每只持仓的入场日期（YYYYMMDD int）
        self._entry_date: dict[str, int] = {}

    # ── on_fills: 记录入场日期 ────────────────────────────────────────────
    def on_fills(self, trades, provider):
        for t in trades:
            if t.side == "BUY":
                self._entry_date[t.symbol] = int(t.date)
            elif t.side == "SELL":
                self._entry_date.pop(t.symbol, None)

    # ── on_tick: 清理状态 ─────────────────────────────────────────────────
    def on_tick(self, bars, snapshot, provider) -> None:
        if not bars:
            return
        current = set(snapshot.holdings.keys())
        stale = [s for s in self._entry_date if s not in current]
        for s in stale:
            del self._entry_date[s]
        # 清理已平仓标的的 trailing 锚点（基类默认 on_tick 负责）
        super().on_tick(bars, snapshot, provider)

    # ── select: 逐仓判断卖出 + 空位补入 ──────────────────────────────────
    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0
        filtered = self.filter_bars(bars, date_str)
        df = bars_to_df(filtered)
        current = set(account_snapshot.holdings.keys())

        if df.empty:
            return {"buy": [], "sell": sorted(current)}

        _, score = eval_factor_specs(df, self.FACTOR_SPECS)
        sorted_score = score.sort_values(ascending=False)

        # ── 逐仓判断卖出 ──
        # 卖出条件：排名 > top_k × sell_rank_mult 且持有 ≥ min_hold_days
        # 与全量轮动的区别：排名在前 top_k 内但持有不足天的不会被卖
        sell_threshold_rank = int(self._top_k * self._sell_rank_mult)
        sell_list = []
        for sym in current:
            if sym not in sorted_score.index:
                sell_list.append(sym)
                continue
            rank = sorted_score.index.get_loc(sym) + 1  # 1-based
            held_days = date_int - self._entry_date.get(sym, date_int)
            if rank > sell_threshold_rank and held_days >= self._min_hold_days:
                sell_list.append(sym)

        # ── 有空位就补入 ──
        # 与全量轮动的区别：空位数 = top_k - (当前持仓 - 卖出数)，
        # 不足 top_k 只补足差额，不卖现有持仓
        slots = self._top_k - (len(current) - len(sell_list))
        buy_list = []
        if slots > 0:
            for sym in sorted_score.index:
                if sym in current and sym not in sell_list:
                    continue
                buy_list.append(sym)
                if len(buy_list) >= slots:
                    break

        # ── provider.get_historical_bars() 示例 ──────────────────────────
        # DataProvider 提供前视保护的日线查——只返回 ≤ 当日的数据。
        # 可用于计算历史排名稳定性、多期动量确认等回溯逻辑。
        # 典型用法：
        #   hist = provider.get_historical_bars(candidates=["000001.SZ"],
        #                                        start=lookback_date, end=date_str)
        #   hist_df = pd.DataFrame(hist).set_index("trade_date")
        # 前视保护自动生效，不需要手动裁剪。

        return {"buy": buy_list, "sell": sell_list}

    # calc_conditions 未覆盖：基类默认实现把 YAML conditions 节翻译为条件单。
