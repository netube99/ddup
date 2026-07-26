"""
示例 4：状态机策略 — on_fills 状态跟踪 + 市场状态检测 + 多模型投票。

展示的核心能力（最全面）：
  ┌─ on_start:
  │   ├── 注册自定义 handler（DYNAMIC_STOP, TIME_STOP, VOLATILITY_EXIT）
  │   ├── 初始化 3 套子模型因子规格（动量/反转/质量）
  │   ├── 初始化市场状态机（bull/bear/neutral）
  │   └── 初始化持仓状态字典（穿透 on_fills / select / calc_conditions）
  │
  ├─ on_fills:
  │   ├── 按 trigger 类型差异化处理（条件止损 vs 手动卖出 vs 条件买入）
  │   ├── 条件止损 → 逐仓冷却期（STOP_LOSS 翻倍冷却）
  │   ├── 手动卖出 → 清理状态
  │   ├── 条件买入 → 初始化持仓跟踪（入场价/最高价/入场日）
  │   └── TRAILING_TP 退出 → 精确感知最高价锚点
  │
  ├─ select:
  │   ├── 市场广度检测 → 状态机切换（连续 N 日确认）
  │   ├── 按市场态动态调整子模型权重
  │   ├── 3 模型独立打分 → 加权合成
  │   ├── buy/sell 名单 + buy_weights + sell_shares
  │   └── buy_conditions (LIMIT_BUY + VWAP_BUY)
  │
  └─ calc_conditions:
      ├── ConditionBuilder 三合一
      ├── DYNAMIC_STOP（波动率自适应）
      ├── TIME_STOP（持仓超 60 天强制退出）
      ├── VOLATILITY_EXIT（日内振幅 > 7% 退出）
      └── holding_days 自适应调参

依赖的自定义因子库：同目录下的 state_machine_factors.yaml

用法：
  python scripts/run.py strategies/examples/state_machine.yaml --start 20240101 --end 20240630
"""
import logging
from typing import Optional

from btcore.filters import StockFilter
from btcore.match.conditions import register_condition_handler
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# 自定义条件单 handler（进程级全局注册）
# ══════════════════════════════════════════════════════════════════════

def _dynamic_stop(holding, cond, bar):
    """波动率自适应止损。"""
    vol_ratio = cond.get("vol_ratio", 0.05)
    width = max(0.03, min(0.10, vol_ratio * 0.02))
    stop_price = holding.entry_price * (1 - width)
    if bar["open"] <= stop_price:
        return (True, bar["open"], {})
    if bar["low"] <= stop_price:
        return (True, stop_price, {})
    return (False, 0.0, {})


def _time_stop(holding, cond, bar):
    """持仓时间止损：持有超过 max_days 日，次日开盘强制退出。"""
    if holding.holding_days >= cond.get("max_days", 999):
        return (True, bar["open"], {"held_days": holding.holding_days})
    return (False, 0.0, {})


def _volatility_exit(holding, cond, bar):
    """日内振幅超阈值退出。"""
    o, h, lo = bar.get("open"), bar.get("high"), bar.get("low")
    if not all([o, h, lo]) or o <= 0:
        return (False, 0.0, {})
    amplitude = (h - lo) / o
    threshold = cond.get("threshold", 0.07)
    if amplitude >= threshold:
        exit_price = max(o * (1 - threshold / 2), lo)
        return (True, exit_price, {"amplitude": round(amplitude, 4)})
    return (False, 0.0, {})


class StateMachine(Strategy):
    """基于市场状态机的多模型投票策略。

    市场状态三态：
      - bull（牛市）：动量权重 0.55，反转 0.15，质量 0.30
      - neutral（震荡）：动量 0.35，反转 0.30，质量 0.35
      - bear（熊市）：动量 0.20，反转 0.50，质量 0.30

    状态切换需要连续 N 日确认（防噪音），切换后调整有效 top_k。
    熊市中有效持仓数减半，降低市场风险暴露。
    """

    # 命令式访问的列（除基础 10 列 + 引擎派生列外）
    REQUIRED_FIELDS: list[str] = ["turnover_rate"]

    def on_start(self, provider, first_date: str, end_date: Optional[str] = None) -> None:
        # ── 注册自定义 handler ──
        register_condition_handler("DYNAMIC_STOP", _dynamic_stop)
        register_condition_handler("TIME_STOP", _time_stop)
        register_condition_handler("VOLATILITY_EXIT", _volatility_exit)

        self._top_k = int(self.config.get("top_k", 8))
        self._cooldown_days = int(self.config.get("cooldown_days", 3))
        self._mode_confirm = int(self.config.get("mode_confirm_days", 5))

        self._filter = StockFilter(
            provider.backend, first_date, self.FILTER_RULES, end_date=end_date
        )
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

        # ═══════════════════════════════════
        # 市场状态机
        # ═══════════════════════════════════
        self._regime: str = "neutral"         # bull | bear | neutral
        self._regime_counter: int = 0          # 连续日数（正=bull倾向，负=bear倾向）
        self._regime_stats: dict[str, int] = {"bull": 0, "bear": 0, "neutral": 0}

        # 仓位乘数：牛市 1.0，震荡 0.85，熊市 0.6
        self._position_mult: dict[str, float] = {"bull": 1.0, "neutral": 0.85, "bear": 0.60}

        # ═══════════════════════════════════
        # 3 套子模型因子规格（eval_factor_specs 用 name 键）
        # ═══════════════════════════════════
        self._momentum_specs = [
            {"name": "mom20_z", "weight": 0.4, "ascending": False},
            {"name": "bias20_z", "weight": 0.3, "ascending": False},
            {"name": "up_days20", "weight": 0.3, "ascending": False},
        ]
        self._reversal_specs = [
            {"name": "mom5_rev", "weight": 0.5, "ascending": True},
            {"name": "bias5", "weight": 0.3, "ascending": True},
            {"name": "near_low20", "weight": 0.2, "ascending": True},
        ]
        self._quality_specs = [
            {"name": "idiosyncratic_vol", "weight": 0.35, "ascending": True},
            {"name": "up_days20", "weight": 0.25, "ascending": False},
            {"name": "vol5_z", "weight": 0.25, "ascending": True},
            {"name": "channel20", "weight": 0.15, "ascending": True},
        ]

        # ═══════════════════════════════════
        # 持仓状态跟踪（穿透 on_fills / select / calc_conditions）
        # ═══════════════════════════════════
        self._holding_state: dict[str, dict] = {}
        self._cooldown_map: dict[str, int] = {}

    # ── on_fills：成交后精确状态跟踪 ──────────────────────────────

    def on_fills(self, trades, provider):
        """感知实际成交，更新持仓状态机。

        - 条件卖出（各类止损/止盈）：进入冷却期，STOP_LOSS 冷却翻倍
        - TYPE_STOP 卖出：额外记录触发原因
        - 手动卖出：清理状态
        - 条件买入：记录入场价和入场日期
        - 所有交易：更新逐仓最高价
        """
        date_int = int(trades[0].date) if trades else 0

        for t in trades:
            if t.side == "SELL":
                # ── 条件卖出 → 冷却期 ──
                if t.trigger in (
                    "STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP",
                    "DYNAMIC_STOP", "TIME_STOP", "VOLATILITY_EXIT",
                ):
                    cd = (
                        self._cooldown_days * 2
                        if t.trigger == "STOP_LOSS" else self._cooldown_days
                    )
                    self._cooldown_map[t.symbol] = date_int + cd

                    # 更新持仓状态（记录退出原因和价格）
                    if t.symbol in self._holding_state:
                        self._holding_state[t.symbol]["exit_trigger"] = t.trigger
                        self._holding_state[t.symbol]["exit_price"] = t.price

                # ── 手动卖出 → 清理 ──
                elif t.trigger == "MANUAL":
                    self._holding_state.pop(t.symbol, None)

            elif t.side == "BUY":
                # ── 初始化/更新持仓状态 ──
                entry = self._holding_state.get(t.symbol, {})
                # 加权均价更新（加仓时）
                old_shares = entry.get("total_shares", 0)
                old_cost = entry.get("entry_price", t.price) * old_shares
                new_cost = t.price * t.shares
                total_shares = old_shares + t.shares
                if total_shares > 0:
                    entry["entry_price"] = (old_cost + new_cost) / total_shares
                else:
                    entry["entry_price"] = t.price
                entry["total_shares"] = total_shares
                entry["entry_date"] = t.date
                entry["highest_price"] = max(entry.get("highest_price", t.price), t.price)
                entry["trigger"] = t.trigger  # 记录买入方式
                self._holding_state[t.symbol] = entry

    # ── select：核心决策 ──────────────────────────────────────────

    def select(self, bars, account_snapshot, provider) -> dict:
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0

        # ── 冷却期递减 ──
        expired = [s for s, d in self._cooldown_map.items() if d <= date_int]
        for s in expired:
            del self._cooldown_map[s]

        # ── 截面过滤 ──
        filtered = self._filter.filter(bars, date_str)
        df = bars_to_df(filtered)
        current = set(account_snapshot.holdings.keys())
        self._cond.prune(current)

        # ── 更新逐仓最高价 ──
        for sym in current:
            if sym in filtered and sym in self._holding_state:
                bar = filtered[sym]
                h = bar.get("high_hfq", bar.get("close", 0)) or 0
                if h > self._holding_state[sym].get("highest_price", 0):
                    self._holding_state[sym]["highest_price"] = h

        # ═══════════════════════════════════
        # 第1步：市场状态检测
        # ═══════════════════════════════════
        try:
            _, breadth_score = eval_factor_specs(df, [
                {"name": "mkt_breadth20", "weight": 0.6, "ascending": False},
                {"name": "mkt_up_ratio", "weight": 0.4, "ascending": False},
            ])
            breadth = breadth_score.mean() if len(breadth_score) > 0 else 0.5
        except Exception:
            breadth = 0.5

        # 状态机：连续 N 日确认才切换
        if breadth > 0.65:
            self._regime_counter = min(self._regime_counter + 1, self._mode_confirm)
        elif breadth < 0.35:
            self._regime_counter = max(self._regime_counter - 1, -self._mode_confirm)
        else:
            self._regime_counter = int(self._regime_counter * 0.5)  # 向零衰减

        old_regime = self._regime
        if self._regime_counter >= self._mode_confirm:
            self._regime = "bull"
        elif self._regime_counter <= -self._mode_confirm:
            self._regime = "bear"
        else:
            self._regime = "neutral"

        if old_regime != self._regime:
            logger.info("Regime switch: %s → %s (breadth=%.3f)", old_regime, self._regime, breadth)
        self._regime_stats[self._regime] += 1

        # ═══════════════════════════════════
        # 第2步：3 模型打分 + 市场态加权
        # ═══════════════════════════════════
        _, mom_score = eval_factor_specs(df, self._momentum_specs)
        _, rev_score = eval_factor_specs(df, self._reversal_specs)
        _, qual_score = eval_factor_specs(df, self._quality_specs)

        weights = {
            "bull": {"momentum": 0.55, "reversal": 0.15, "quality": 0.30},
            "neutral": {"momentum": 0.35, "reversal": 0.30, "quality": 0.35},
            "bear": {"momentum": 0.20, "reversal": 0.50, "quality": 0.30},
        }[self._regime]

        total_score = (
            mom_score.fillna(0) * weights["momentum"]
            + rev_score.fillna(0) * weights["reversal"]
            + qual_score.fillna(0) * weights["quality"]
        )

        # ═══════════════════════════════════
        # 第3步：选股 + 调仓
        # ═══════════════════════════════════
        effective_top_k = max(2, int(self._top_k * self._position_mult[self._regime]))
        total_score = total_score[~total_score.index.isin(self._cooldown_map)]

        sorted_score = total_score.sort_values(ascending=False)
        target = set(sorted_score.head(effective_top_k).index)
        buy_list = sorted(target - current)
        sell_list = sorted(current - target)

        # ── buy_weights ──
        buy_weights = {}
        if buy_list:
            raw = total_score.loc[buy_list].clip(lower=0)
            s = raw.sum()
            if s > 0:
                for sym in buy_list:
                    buy_weights[sym] = float(raw[sym] / s * self._position_mult[self._regime])

        # ── sell_shares：部分减仓持仓但得分尚可的 ──
        sell_shares = {}
        near_border = set(sorted_score.head(int(self._top_k * 1.3)).index)
        for sym in list(sell_list):
            if sym in near_border:
                h = account_snapshot.holdings.get(sym)
                if h and h.shares >= 200:
                    sell_shares[sym] = h.shares // 2
                    sell_list.remove(sym)  # 从完全清仓中移除

        # ── buy_conditions ──
        buy_conditions = []
        hunt = sorted_score.iloc[effective_top_k:effective_top_k + 3]
        for sym in hunt.index:
            if sym in sell_list or sym in self._cooldown_map:
                continue
            bar = filtered.get(sym, {})
            close = bar.get("close", 0) or 0
            if close > 0:
                hunt_value = account_snapshot.total_value * 0.02
                buy_conditions.append({
                    "symbol": sym, "type": "LIMIT_BUY",
                    "price": round(close * 0.97, 2), "value": hunt_value,
                })

        return {
            "buy": buy_list,
            "sell": sell_list,
            "buy_weights": buy_weights if buy_weights else None,
            "sell_shares": sell_shares if sell_shares else None,
            "buy_conditions": buy_conditions if buy_conditions else None,
        }

    # ── calc_conditions：多层条件单系统 ────────────────────────────

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """每个持仓每日生成的条件单。

        - ConditionBuilder: STOP_LOSS + TAKE_PROFIT + TRAILING_TP
        - DYNAMIC_STOP: 波动率自适应，替代标准止损
        - TIME_STOP: 持仓超 60 日强制退出
        - VOLATILITY_EXIT: 日内振幅超 7% 退出
        """
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)

        # ── 波动率自适应止损 ──
        pct_chg = bar.get("pct_chg", 0) or 0
        turnover = bar.get("turnover_rate", 0) or 0
        est_vol = abs(pct_chg) * (1 + min(turnover, 0.10) * 10)
        conds.append({
            "symbol": symbol, "type": "DYNAMIC_STOP",
            "price": None, "vol_ratio": min(est_vol, 0.15),
        })

        # ── 持仓超时止损 ──
        conds.append({
            "symbol": symbol, "type": "TIME_STOP",
            "price": None, "max_days": 60,
        })

        # ── 异常波动退出 ──
        conds.append({
            "symbol": symbol, "type": "VOLATILITY_EXIT",
            "price": None, "threshold": 0.07,
        })

        # ── holding_days 动态调参 ──
        for c in conds:
            if c.get("type") == "STOP_LOSS":
                if holding_days <= 3:
                    c["price"] = entry_price * 0.97  # 新仓保护
                elif holding_days > 30:
                    c["price"] = entry_price * 0.85  # 老仓放宽
            elif c.get("type") == "TAKE_PROFIT" and holding_days <= 3:
                conds.remove(c)

        return conds
