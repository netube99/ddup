"""训练标签构建。

panel 模型：xs_forward_return — 每日截面内 N 日前向收益（hfq 口径）的
pct rank ∈ (0,1]，消除市场 beta，跨日可比；同时返回原始前向收益
（供分层评估，逐日单调等价于 rank 标签）。

holding scope 模型：trend_break — 从回测结果库 trade_log FIFO 配对买卖，
持仓期间逐日打标：未来 lookahead 天内触发 TREND_BREAK 且净亏损 = 正样本。
账户态特征（hold_days / ret_from_entry）按持仓区间重放，
公式与引擎推理侧共用 btcore.ml.runtime.compute_state_features。
"""

import logging
import sqlite3
from collections import defaultdict

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
    """从结果库 trade_log FIFO 配对买卖，返回持仓回合表。

    列: symbol, buy_date, sell_date, buy_price, pnl, trigger, holding_days。
    """
    db = sqlite3.connect(result_db_path)
    buys = defaultdict(list)
    for row in db.execute(
        "SELECT symbol, date, price, shares, turnover, net_amount "
        "FROM trade_log WHERE side='BUY' AND trigger='MANUAL' ORDER BY date"
    ):
        buys[row[0]].append(
            dict(zip(["date", "price", "shares", "turnover", "net_amount"], row[1:]))
        )
    sells = [
        dict(zip(
            ["symbol", "date", "price", "shares", "turnover", "net_amount", "trigger"],
            row,
        ))
        for row in db.execute(
            "SELECT symbol, date, price, shares, turnover, net_amount, trigger "
            "FROM trade_log WHERE side='SELL' ORDER BY date"
        )
    ]
    db.close()

    buy_queue = {k: list(v) for k, v in buys.items()}
    pairs = []
    for sell in sells:
        sym = sell["symbol"]
        if sym not in buy_queue or not buy_queue[sym]:
            continue
        buy = buy_queue[sym].pop(0)
        hd = (pd.to_datetime(sell["date"]) - pd.to_datetime(buy["date"])).days
        pairs.append({
            "symbol": sym,
            "buy_date": buy["date"],
            "sell_date": sell["date"],
            "buy_price": buy["price"],
            "pnl": round(sell["net_amount"] + buy["net_amount"], 2),
            "trigger": sell["trigger"],
            "holding_days": hd,
        })
    return pd.DataFrame(pairs)


def build_guard_samples(
    panel: pd.DataFrame,
    pairs_df: pd.DataFrame,
    spec: ModelSpec,
    lookahead: int,
) -> pd.DataFrame:
    """holding scope 训练样本：持仓期间逐日一行。

    - positive: 该回合以 TREND_BREAK 触发且净亏损，且当日距卖出 ∈ [1, lookahead]
    - negative: 非 TB 亏损回合的持仓日（末尾 lookahead 天丢弃，避免边界混淆），
      以及 TB 亏损回合的"安全窗口"日
    特征 = 面板特征列 + 账户态特征（按统一公式重放）。
    """
    feature_cols = spec.feature_order
    samples = []
    for pos in pairs_df.itertuples(index=False):
        sym = pos.symbol
        mask = (
            (panel.index.get_level_values("symbol") == sym)
            & (panel.index.get_level_values("trade_date") >= pos.buy_date)
            & (panel.index.get_level_values("trade_date") <= pos.sell_date)
        )
        pos_bars = panel.loc[mask].sort_index(level="trade_date")
        if len(pos_bars) < 3:
            continue

        is_positive_pair = pos.trigger == "TREND_BREAK" and pos.pnl < 0
        dates = pos_bars.index.get_level_values("trade_date")

        for i in range(len(pos_bars)):
            trade_date = dates[i]
            hd = (pd.to_datetime(trade_date) - pd.to_datetime(pos.buy_date)).days
            if hd < 1:
                continue
            dts = (pd.to_datetime(pos.sell_date) - pd.to_datetime(trade_date)).days

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
                row[name] = float(v) if v is not None and v == v else 0.0
            samples.append(row)

    return pd.DataFrame(samples)
