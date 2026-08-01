"""
示例 6: self_managed_time — 时间门控 + 非对称买卖。

展示能力：
  - 策略代码自管理调仓间隔
  - 非调仓日只卖不买（非对称控制）
  - on_fills 成交感知 → 条件单卖出后冷却
  - on_tick 每日状态维护

模式：时间门控
  - 调仓日完整轮动，非调仓日仅紧急卖出
  - 与 self_managed_rank（排名阈值模式）形成两种自管理方案
"""

from btcore.strategy import Strategy
from btcore.strategy_tools import bars_to_df, eval_factor_specs


class SelfManagedTime(Strategy):
    """时间门控换手：调仓日全量轮动，非调仓日只卖不买。

    声明式一刀切拦截 select 会让买和卖都为空——
    而非调仓日本策略仍然允许紧急卖出。
    """

    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        super().on_start(provider, first_date, end_date)
        self._top_k = int(self.config.get("top_k", 5))
        self._rebalance_interval = int(self.config.get("rebalance_interval", 22))
        self._emergency_sell_threshold = float(
            self.config.get("emergency_sell_threshold", -0.09)
        )

        # 冷却期 + 上次调仓日
        self._cooldown: dict[str, int] = {}
        self._last_rebalance: int = 0

    # ── on_fills: 条件单卖出冷却 ──────────────────────────────────────────
    def on_fills(self, trades, provider):
        for t in trades:
            if t.side == "SELL" and t.trigger in (
                "STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP"
            ):
                # STOP_LOSS 信号更强，冷却更久
                cd = 20 if t.trigger == "STOP_LOSS" else 10
                self._cooldown[t.symbol] = int(t.date) + cd

    # ── on_tick: 每日维护 ─────────────────────────────────────────────────
    def on_tick(self, bars, snapshot, provider) -> None:
        if not bars:
            return
        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0

        # 冷却期到期清理
        # 冷却期到期清理
        expired = [s for s, d in self._cooldown.items() if d <= date_int]
        for s in expired:
            del self._cooldown[s]

        # 清理已平仓标的的 trailing 锚点（基类默认 on_tick 负责）
        super().on_tick(bars, snapshot, provider)

    # ── select: 时间门控调仓 ─────────────────────────────────────────────
    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0
        filtered = self.filter_bars(bars, date_str)
        df = bars_to_df(filtered)
        current = set(account_snapshot.holdings.keys())

        # 距离上次调仓是否已够间隔
        is_rebalance_day = (date_int - self._last_rebalance) >= self._rebalance_interval

        # ── 非调仓日：只检测紧急卖出，不新买 ────────────────────────────
        # 本策略主动控制：卖随时可触发，买只在固定间隔。
        if not is_rebalance_day:
            emergency_sells = []
            for sym in current:
                bar = filtered.get(sym, {})
                pct = bar.get("pct_chg")
                if pct is not None and isinstance(pct, (int, float)):
                    if pct <= self._emergency_sell_threshold:
                        emergency_sells.append(sym)
            return {"buy": [], "sell": emergency_sells}

        # ── 调仓日：完整轮动 ────────────────────────────────────────────
        self._last_rebalance = date_int

        if df.empty:
            return {"buy": [], "sell": sorted(current)}

        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # 排除冷却期标的
        score = score[~score.index.isin(self._cooldown)]

        target = set(score.sort_values(ascending=False).head(self._top_k).index)
        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        return {"buy": buy_list, "sell": sell_list}

    # calc_conditions 未覆盖：基类默认实现把 YAML conditions 节翻译为条件单。
