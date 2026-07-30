"""
示例 4: multi_model — 多模型投票 + 市场状态机 + 域分离。

展示能力：
  - 多子模型独立打分 → 按市场状态加权合成
  - 市场状态机（广度检测 → 推迟确认 → 权重切换）
  - 自定义因子库 factor_library
  - 域分离（index_universe / factor_universe + 手动 get_universe 覆盖）
  - 自定义条件单 handler（DYNAMIC_STOP / TIME_STOP）
  - 时间门控调仓 + on_tick 每日状态推进
  - REQUIRED_FIELDS 声明非因子列
  - materialize_only 因子在 calc_conditions 中使用

下一级 self_managed_rank / self_managed_time 展示自管理换手的两种模式。
"""

from btcore.filters import StockFilter
from btcore.match.conditions import register_condition_handler
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

# ══════════════════════════════════════════════════════════════════════════
# 自定义条件单 handler
# ══════════════════════════════════════════════════════════════════════════

def _dynamic_stop(holding, cond, bar):
    """波动率自适应止损。"""
    width = max(0.03, min(0.10, cond.get("vol_ratio", 0.05) * 0.02))
    stop_price = holding.entry_price * (1 - width)
    if bar["open"] <= stop_price:
        return (True, bar["open"], {"width": round(width, 4)})
    if bar["low"] <= stop_price:
        return (True, stop_price, {"width": round(width, 4)})
    return (False, 0.0, {})


def _time_stop(holding, cond, bar):
    """持仓超时强退：超过 max_days 天未触发任何条件单，主动平仓。"""
    threshold = cond.get("max_days", 60)
    if holding.holding_days >= threshold:
        return (True, bar["open"], {"days": holding.holding_days})
    return (False, 0.0, {})


# ══════════════════════════════════════════════════════════════════════════
# MultiModel 策略
# ══════════════════════════════════════════════════════════════════════════

class MultiModel(Strategy):
    """多模型投票 + 市场状态机，时间门控调仓。

    三套子模型（动量 / 反转 / 质量）各自对候选池打分，
    最终得分 = 各模型得分 × 市场状态权重 的加权和。
    市场状态由广度指标决定，连续 N 日确认后切换。

    select() 每日运行，策略代码自行管理调仓节奏：
    on_tick 每日更新状态机，select 在调仓日执行多模型加权投票。

    域分离：交易限于沪深300，因子计算域扩展到中证800——
    截面排名和坍缩聚合有更宽的参照池。
    """

    # ── REQUIRED_FIELDS ──────────────────────────────────────────────────
    # on_tick 中命令式访问 turnover_rate，必须声明以确保引擎保留该列
    REQUIRED_FIELDS: list[str] = ["turnover_rate"]

    # ── 市场状态枚举 ─────────────────────────────────────────────────────
    BULL = "bull"
    BEAR = "bear"
    NEUTRAL = "neutral"

    # 状态 → 三模型权重 (动量, 反转, 质量)
    MODE_WEIGHTS = {
        "bull": (0.55, 0.15, 0.30),    # 牛市：动量主导
        "neutral": (0.35, 0.30, 0.35),  # 震荡：均衡
        "bear": (0.20, 0.50, 0.30),     # 熊市：反转主导
    }

    # ── get_universe: 手动覆盖 vs YAML 自动生成 ──────────────────────────
    def get_universe(self, provider, start: str, end: str) -> list[str] | None:
        """手动返回交易域——覆盖 YAML index_universe 的自动生成版本。

        两种写法效果相同，选其一：
          A) YAML 声明 index_universe（loader 自动生成此方法）
          B) 手动覆盖此方法（本示例）
        同时使用两者时，手动覆盖优先。

        此处手动实现展示 get_index_members 的调用模式。
        """
        if not hasattr(provider.backend, "get_index_members"):
            return None  # 后端不支持 → 全市场
        from datetime import date as dt_date
        from datetime import timedelta
        lookback = (dt_date.fromisoformat(start) - timedelta(days=45)).strftime("%Y%m%d")
        snapshots = provider.backend.get_index_members(["000300.SH"], lookback, end)
        if not snapshots:
            return None
        return sorted(set().union(*snapshots.values()))

    # ── get_factor_universe: 因子计算域 ───────────────────────────────────
    def get_factor_universe(self, provider, start: str, end: str) -> list[str] | None:
        """因子计算域——比交易域更宽，提供更丰富的截面参照。

        交易域是沪深300，因子在中证800上计算：
          - zscore/rank 在中证800口径上排名（更宽池子，更稳定）
          - mean/group_mean 坍缩算子在中证800全量聚合，信号更有统计意义
        """
        if not hasattr(provider.backend, "get_index_members"):
            return None
        from datetime import date as dt_date
        from datetime import timedelta
        lookback = (dt_date.fromisoformat(start) - timedelta(days=45)).strftime("%Y%m%d")
        snapshots = provider.backend.get_index_members(["000906.SH"], lookback, end)
        if not snapshots:
            return None
        return sorted(set().union(*snapshots.values()))

    # ── on_start ─────────────────────────────────────────────────────────
    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        # 注册自定义 handler
        register_condition_handler("DYNAMIC_STOP", _dynamic_stop)
        register_condition_handler("TIME_STOP", _time_stop)

        self._top_k = int(self.config.get("top_k", 8))
        self._cooldown_days = int(self.config.get("cooldown_days", 3))
        self._confirm_days = int(self.config.get("mode_confirm_days", 5))
        self._rebalance_interval = int(self.config.get("rebalance_interval", 5))
        self._last_rebalance = 0

        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

        # 三套子模型的因子规格（各自独立打分）
        self._momentum_specs = [
            {"name": "mom20_z", "weight": 1.0, "ascending": False},
            {"name": "up_days20", "weight": 0.5, "ascending": False},
        ]
        self._reversal_specs = [
            {"name": "mom5_rev", "weight": 1.0, "ascending": True},
            {"name": "near_low20", "weight": 1.0, "ascending": True},
            {"name": "bias5", "weight": 0.5, "ascending": True},
        ]
        self._quality_specs = [
            {"name": "vol5_z", "weight": 1.0, "ascending": True},
            {"name": "channel20", "weight": 0.5, "ascending": True},
            {"name": "idiosyncratic_vol", "weight": 0.5, "ascending": True},
        ]

        # 市场状态机
        self._mode = self.NEUTRAL
        self._mode_counter = 0  # 连续确认计数
        self._cooldown: dict[str, int] = {}

        # 持仓跟踪
        self._entry_price: dict[str, float] = {}
        self._holding_high: dict[str, float] = {}

    # ── on_fills: 成交感知 ────────────────────────────────────────────────
    def on_fills(self, trades, provider):
        for t in trades:
            date_int = int(t.date)
            if t.side == "BUY":
                # 买入：记录入场价（后续 calc_conditions 可用）
                self._entry_price[t.symbol] = t.price
                self._holding_high[t.symbol] = t.price
            elif t.side == "SELL":
                # 条件单卖出 → 冷却期
                if t.trigger in ("STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP",
                                 "DYNAMIC_STOP", "TIME_STOP"):
                    self._cooldown[t.symbol] = date_int + self._cooldown_days
                # 清理跟踪状态
                self._entry_price.pop(t.symbol, None)
                self._holding_high.pop(t.symbol, None)

    # ── on_tick: 每日状态推进 ────────────────────────────────────────────
    def on_tick(self, bars, snapshot, provider) -> None:
        if not bars:
            return

        # 冷却期递减
        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0
        expired = [s for s, d in self._cooldown.items() if d <= date_int]
        for s in expired:
            del self._cooldown[s]

        # 逐仓最高价更新（用于 trailing 等逻辑）
        for sym, h in snapshot.holdings.items():
            bar = bars.get(sym, {})
            high_hfq = bar.get("high_hfq", bar.get("high", 0))
            if high_hfq > self._holding_high.get(sym, 0):
                self._holding_high[sym] = high_hfq

        # ── 市场状态机推进 ──────────────────────────────────────────────
        # 用坍缩因子 mkt_breadth20（同日所有股票同值）检测市场广度
        # 广度 > 0.65 → 偏牛；< 0.35 → 偏熊；否则中性
        bar_sample = next(iter(bars.values()))
        breadth = bar_sample.get("mkt_breadth20")
        up_ratio = bar_sample.get("mkt_up_ratio")

        if breadth is not None and up_ratio is not None:
            if breadth > 0.65 and up_ratio > 0.55:
                target_mode = self.BULL
            elif breadth < 0.35 and up_ratio < 0.45:
                target_mode = self.BEAR
            else:
                target_mode = self.NEUTRAL

            # 推迟确认：连续 confirm_days 日同向才切换
            if target_mode == self._mode:
                self._mode_counter = 0
            else:
                self._mode_counter += 1
                if self._mode_counter >= self._confirm_days:
                    self._mode = target_mode
                    self._mode_counter = 0

        # 修剪条件单状态
        self._cond.prune(set(snapshot.holdings.keys()))

    # ── select: 多模型加权投票（时间门控） ──────────────────────────────
    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0

        # ── 时间门控：非调仓日不操作 ───────────────────────────────────
        # 状态机在 on_tick 中每日更新，不受调仓节奏影响。
        is_rebalance_day = (date_int - self._last_rebalance) >= self._rebalance_interval
        if not is_rebalance_day:
            return {"buy": [], "sell": []}

        self._last_rebalance = date_int

        filtered = self._filter.filter(bars, date_str)
        df = bars_to_df(filtered)

        # ── 三模型独立打分 ──────────────────────────────────────────────
        # 动量模型：偏好高动量
        _, mom_score = eval_factor_specs(df, self._momentum_specs)
        # 反转模型：偏好超跌反弹
        _, rev_score = eval_factor_specs(df, self._reversal_specs)
        # 质量模型：偏好低波 + 低特质波动
        _, qual_score = eval_factor_specs(df, self._quality_specs)

        # ── 按市场状态加权合成 ──────────────────────────────────────────
        w_mom, w_rev, w_qual = self.MODE_WEIGHTS[self._mode]
        composite = (
            mom_score * w_mom + rev_score * w_rev + qual_score * w_qual
        )

        # ── 仓位乘数 ─────────────────────────────────────────────────────
        # 牛市中激进（满 top_k），熊市中保守（减半）
        eff_top_k = self._top_k
        if self._mode == self.BEAR:
            eff_top_k = max(2, self._top_k // 2)

        # 排除冷却期
        composite = composite[~composite.index.isin(self._cooldown)]

        sorted_score = composite.sort_values(ascending=False)
        target = set(sorted_score.head(eff_top_k).index)
        current = set(account_snapshot.holdings.keys())

        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # ── buy_weights 按合成得分比例 ──────────────────────────────────
        buy_weights = None
        if buy_list:
            raw = sorted_score.loc[buy_list].clip(lower=0)
            total = raw.sum()
            if total > 0:
                buy_weights = {sym: float(raw[sym] / total * 0.9) for sym in buy_list}

        return {"buy": buy_list, "sell": sell_list, "buy_weights": buy_weights}

    # ── calc_conditions: 多类型条件单 ────────────────────────────────────
    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        # 动态止损（用当日波动估算宽度）
        pct_chg = bar.get("pct_chg", 0) or 0
        turnover = bar.get("turnover_rate", 0) or 0
        est_vol = abs(pct_chg) * (1 + min(turnover, 0.10) * 10)
        conds.append({
            "type": "DYNAMIC_STOP",
            "price": None,
            "vol_ratio": min(est_vol, 0.15),
        })

        # 持仓超时强退
        conds.append({"type": "TIME_STOP", "price": None, "max_days": 60})

        # holding_days 自适应：新仓紧止损
        for c in conds:
            if c.get("type") == "STOP_LOSS" and holding_days <= 3:
                c["price"] = entry_price * 0.97
            elif c.get("type") == "TAKE_PROFIT" and holding_days <= 3:
                conds.remove(c)

        return conds
