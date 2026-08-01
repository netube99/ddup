"""训练标签构建。

panel 模型：xs_forward_return — 每日截面内 N 日前向收益（hfq 口径）的
pct rank ∈ (0,1]，消除市场 beta，跨日可比；同时返回原始前向收益
（供分层评估，逐日单调等价于 rank 标签）。

holding scope 模型：trend_break — 从回测结果库 trade_log 重构完整持仓
回合（不限买入 trigger），持仓期间逐日打标：未来 lookahead 个交易日内
触发 TREND_BREAK 且净亏损 = 正样本。账户态特征（hold_days /
ret_from_entry）按持仓区间重放，公式与引擎推理侧共用
btcore.ml.runtime.compute_state_features；hold_days 按市场交易日口径
（引擎逐日 +1，成交当日 decision 时点为 1），缺失特征保留 NaN，由
trainer 在 scaler 之后填 0（= 训练段均值）。
"""

import logging
import sqlite3

import numpy as np
import pandas as pd

from btcore.ml.runtime import compute_state_features
from btcore.ml.spec import ModelSpec
from btcore.types import Holding

logger = logging.getLogger(__name__)


def xs_forward_return(panel: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """截面前向收益标签。

    Returns:
        DataFrame（同 panel 索引）：label = 每日截面 pct rank ∈ (0,1]；
        fwd_ret = 原始 N 日前向收益。尾部 horizon 天为 NaN（调用方 dropna）。
    """
    if horizon < 1:
        raise ValueError(f"horizon 必须 >= 1: {horizon}")
    close = panel["close_hfq"]
    fwd = close.groupby(level="symbol", sort=False).shift(-horizon) / close - 1.0
    label = fwd.groupby(level="trade_date", sort=False).rank(pct=True)
    return pd.DataFrame({"label": label, "fwd_ret": fwd}, index=panel.index)


def extract_trade_pairs(result_db_path: str) -> pd.DataFrame:
    """从结果库 trade_log 重构完整持仓回合表（回合 = 持仓 0 → 归 0）。

    买入不限 trigger（MANUAL / TARGET / 条件买入均计入——此前只认
    MANUAL 会把 target_value 与条件买入策略的回合静默蒸发成空标签集）；
    回合内多买多卖按股数累计，buy_price = 加权均价，pnl = 回合全部净额
    之和，trigger = 最后一笔卖出的 trigger（TREND_BREAK 判定依据）。
    残缺回合（卖无买 / 卖出超买 / 期末未平仓）跳过并告警——静默丢弃
    会产出错误标签，告警是下限。

    列: symbol, buy_date, sell_date, buy_price, pnl, trigger。
    """
    db = sqlite3.connect(result_db_path)
    rows = db.execute(
        "SELECT symbol, date, side, trigger, price, shares, net_amount "
        "FROM trade_log ORDER BY date, id"
    ).fetchall()
    db.close()

    open_rounds: dict[str, dict] = {}
    rounds: list[dict] = []
    for symbol, date, side, trigger, price, shares, net_amount in rows:
        if side == "BUY":
            r = open_rounds.get(symbol)
            if r is None:
                r = open_rounds[symbol] = {
                    "symbol": symbol, "shares": 0, "buy_date": date,
                    "buy_shares": 0, "buy_cost": 0.0, "pnl": 0.0,
                    "sell_date": None, "trigger": None,
                }
            r["shares"] += shares
            r["buy_shares"] += shares
            r["buy_cost"] += shares * price
            r["pnl"] += net_amount
        elif side == "STK_DIV":
            # 送转增股：trade_log 的 shares = 送转后总股数。buy_shares 同步为
            # 总股数，buy_price = buy_cost / buy_shares 即除权后每股成本
            #（引擎 entry_price 同口径），否则卖出超买被误判残缺回合
            r = open_rounds.get(symbol)
            if r is None:
                logger.warning(
                    "[ML标签] %s %s 送转无对应持仓，跳过", date, symbol,
                )
                continue
            r["shares"] = shares
            r["buy_shares"] = shares
        else:  # SELL
            r = open_rounds.get(symbol)
            if r is None:
                logger.warning(
                    "[ML标签] %s %s 卖出无对应买入，残缺回合跳过", date, symbol,
                )
                continue
            r["shares"] -= shares
            r["pnl"] += net_amount
            r["sell_date"] = date
            r["trigger"] = trigger
            if r["shares"] <= 0:
                del open_rounds[symbol]
                if r["shares"] < 0:
                    logger.warning(
                        "[ML标签] %s %s 卖出股数超过买入（超卖 %d 股），"
                        "残缺回合跳过", date, symbol, -r["shares"],
                    )
                    continue
                rounds.append({
                    "symbol": symbol,
                    "buy_date": r["buy_date"],
                    "sell_date": r["sell_date"],
                    "buy_price": round(r["buy_cost"] / r["buy_shares"], 4),
                    "pnl": round(r["pnl"], 2),
                    "trigger": r["trigger"],
                })
    for symbol, r in open_rounds.items():
        logger.warning(
            "[ML标签] %s 期末未平仓回合跳过（%d 股）", symbol, r["shares"],
        )
    return pd.DataFrame(rounds)


def build_guard_samples(
    panel: pd.DataFrame,
    pairs_df: pd.DataFrame,
    spec: ModelSpec,
    lookahead: int,
) -> pd.DataFrame:
    """holding scope 训练样本：持仓期间逐日一行。

    - positive: 该回合以 TREND_BREAK 触发且净亏损，且当日距卖出 ∈ [1, lookahead] 个交易日
    - negative: 非 TB 亏损回合的持仓日（末尾 lookahead 个交易日丢弃，避免边界混淆），
      以及 TB 亏损回合的"安全窗口"日
    特征 = 面板特征列 + 账户态特征（按统一公式重放；hold_days 为市场
    交易日口径，与引擎 decision 时点的 holding.holding_days 逐日一致）。
    """
    feature_cols = spec.feature_order
    samples = []
    # 市场交易日位置表：hold_days / 距卖出天数都按交易日计，不能用
    # 日历日（约 1.43 倍漂移，且与引擎逐日 +1 的口径不一致）
    cal = panel.index.get_level_values("trade_date").unique().sort_values()
    date_pos = {d: k for k, d in enumerate(cal)}
    # 预按 symbol 分组：避免每回合对全面板构造布尔掩码（O(回合数 × 面板)）
    groups = {
        sym: g for sym, g in panel.groupby(level="symbol", sort=False)
    }
    for pos in pairs_df.itertuples(index=False):
        sym = pos.symbol
        g = groups.get(sym)
        if g is None:
            continue
        dts = g.index.get_level_values("trade_date")
        pos_bars = g[(dts >= pos.buy_date) & (dts <= pos.sell_date)]
        if len(pos_bars) < 3:
            continue

        is_positive_pair = pos.trigger == "TREND_BREAK" and pos.pnl < 0
        dates = pos_bars.index.get_level_values("trade_date")
        buy_pos = date_pos.get(pos.buy_date)
        sell_pos = date_pos.get(pos.sell_date)

        for i in range(len(pos_bars)):
            trade_date = dates[i]
            # 引擎在成交当日的 _compute_pending 已 +1：成交日 holding_days=1。
            # buy_date 落在面板窗口外时退化为窗口内相对位置（近似）
            hd = date_pos[trade_date] - buy_pos + 1 if buy_pos is not None else i + 1
            dts = (
                sell_pos - date_pos[trade_date]
                if sell_pos is not None
                else len(pos_bars) - 1 - i
            )

            if is_positive_pair:
                label = 1 if 1 <= dts <= lookahead else 0
            else:
                if dts <= lookahead:
                    continue
                label = 0

            day = pos_bars.iloc[i]
            # 账户态特征重放：与引擎推理侧同一公式
            holding = Holding(
                symbol=sym, shares=100, entry_date=pos.buy_date,
                entry_price=pos.buy_price, cost=pos.buy_price * 100,
                holding_days=hd,
            )
            bar = day.to_dict()
            row = {"label": label, "trade_date": trade_date}
            state = compute_state_features(spec.state_features, bar, holding)
            for name in feature_cols:
                v = state.get(name, day.get(name))
                # 缺失保留 NaN：trainer 在 scaler 之后填 0（训练段均值）
                row[name] = float(v) if v is not None and v == v else np.nan
            samples.append(row)

    return pd.DataFrame(samples)
