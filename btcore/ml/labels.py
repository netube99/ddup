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

import duckdb
import pandas as pd

from btcore import database
from btcore.ml.runtime import assemble_feature_value, compute_state_features
from btcore.ml.spec import ModelSpec
from btcore.types import Holding

# 回合重放的同日时序 = 引擎盘中执行序：公司行为(盘前)→卖出→买入
# （stats 的 FIFO 撮合对同日买卖序不敏感，TRADE_EVENT_PRIORITY 保持不动）
_REPLAY_EVENT_PRIORITY = {"DIV": 0, "STK_DIV": 0, "ADJUST": 0, "SELL": 1, "BUY": 2}

logger = logging.getLogger(__name__)

# trade_log 列名常量（本文件内单一来源）：与 btcore.database.SCHEMA_SQL 的
# canonical schema 对应（id/run_id 仅用于排序与过滤，不入列集）。本轮不跨
# 文件统一（constants.py 归属 S2 维护），列名变更需与 database.py 同步。
TRADE_LOG_SELECT_COLS = (
    "symbol", "date", "side", "trigger", "price", "shares", "net_amount",
)


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


def _resolve_run_id(db: duckdb.DuckDBPyConnection, run_id: int | None) -> int:
    """run_id 缺省解析：优先最新 completed run，无 completed 回退最新 run。"""
    if run_id is not None:
        return run_id
    has_runs = db.execute(
        "SELECT 1 FROM information_schema.tables WHERE table_name = 'runs'"
    ).fetchone()
    if has_runs is None:
        raise ValueError("结果库缺少 runs 表，无法定位 trade_log 所属 run")
    n_runs = db.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if n_runs == 0:
        raise ValueError("结果库 runs 表为空，无可用回测 run")
    completed = db.execute(
        "SELECT MAX(run_id) FROM runs WHERE status = 'completed'"
    ).fetchone()[0]
    if completed is not None:
        chosen = completed
    else:
        chosen = db.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
    if n_runs > 1:
        logger.warning(
            "[ML标签] 结果库含 %d 个 run，缺省取 run_id=%d（--run-id 可指定）",
            n_runs, chosen,
        )
    return chosen


def extract_trade_pairs(result_db_path: str, run_id: int | None = None) -> pd.DataFrame:
    """从结果库 trade_log 重构完整持仓回合表（回合 = 持仓 0 → 归 0）。

    run_id：多 run 结果库必须定位单一 run，否则跨 run 交易混入会腐化
    回合。缺省取最新 status='completed' 的 run（无 completed 回退最新
    run），多 run 时告警告知所选；runs 表缺失/为空抛 ValueError。

    同日时序：按 (date, TRADE_EVENT_PRIORITY) 稳定重排（Python sorted
    稳定，同 (date, priority) 内保持 id 落库序）——公司行为（盘前）
    先于买卖处理。DIV 红利计入在持回合 pnl（除息日清仓场景 DIV 先于
    SELL，红利归属该回合）；无持仓的红利告警跳过。ADJUST（实盘账本
    现金审计行）非持仓事件，静默跳过不进回合。

    买入不限 trigger（MANUAL / TARGET / 条件买入均计入——此前只认
    MANUAL 会把 target_value 与条件买入策略的回合静默蒸发成空标签集）；
    回合内多买多卖按股数累计，buy_price = 加权均价，pnl = 回合全部净额
    之和，trigger = 最后一笔卖出的 trigger（TREND_BREAK 判定依据）。
    残缺回合（卖无买 / 卖出超买 / 期末未平仓）跳过并告警——静默丢弃
    会产出错误标签，告警是下限。

    列: symbol, buy_date, sell_date, buy_price, pnl, trigger, events。
    events = 回合内按 (date, 优先级) 序的原始事件行
    (date, side, price, shares, net_amount)——build_guard_samples 据此重放
    引擎 Holding 状态（entry_price 逐日路径），静态聚合 buy_price 无法
    表达加仓重算与期内除权除息 rescale。
    """
    db = database.connect_result_db(result_db_path, read_only=True)
    run_id = _resolve_run_id(db, run_id)
    rows = db.execute(
        "SELECT " + ", ".join(TRADE_LOG_SELECT_COLS)
        + " FROM trade_log WHERE run_id = ? ORDER BY date, id",
        (run_id,),
    ).fetchall()
    db.close()
    # 同日按引擎盘中执行序重排：公司行为(盘前)→卖出→买入；稳定排序，
    # 同键内保持 id 序。引擎盘中先卖后买（engine.step 手动卖 → 手动买），
    # 同日同票的「条件卖出 + 再买入」必须拆成两个回合——stats 的 FIFO
    # 撮合对同日买卖序不敏感，TRADE_EVENT_PRIORITY（买入→卖出）保持不动
    rows = sorted(
        rows, key=lambda r: (r[1], _REPLAY_EVENT_PRIORITY.get(r[2], 3)),
    )

    open_rounds: dict[str, dict] = {}
    rounds: list[dict] = []
    for symbol, date, side, trigger, price, shares, net_amount in rows:
        if side == "BUY":
            r = open_rounds.get(symbol)
            if r is None:
                r = open_rounds[symbol] = {
                    "symbol": symbol, "shares": 0, "buy_date": date,
                    "buy_shares": 0, "buy_cost": 0.0, "pnl": 0.0,
                    "sell_date": None, "trigger": None, "events": [],
                }
            r["events"].append((date, side, price, shares, net_amount))
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
            r["events"].append((date, side, price, shares, net_amount))
        elif side == "DIV":
            # 红利现金：同日重排后 DIV 先于 SELL 处理，除息日清仓时
            # 红利计入该回合 pnl；无在持回合 = 数据残缺，告警跳过
            r = open_rounds.get(symbol)
            if r is None:
                logger.warning(
                    "[ML标签] %s %s 红利无对应持仓，跳过", date, symbol,
                )
                continue
            r["events"].append((date, side, price, shares, net_amount))
            r["pnl"] += net_amount
        elif side == "ADJUST":
            # 实盘账本现金审计行（非持仓事件），不进回合
            continue
        else:  # SELL
            r = open_rounds.get(symbol)
            if r is None:
                logger.warning(
                    "[ML标签] %s %s 卖出无对应买入，残缺回合跳过", date, symbol,
                )
                continue
            r["events"].append((date, side, price, shares, net_amount))
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
                    "events": r["events"],
                })
    for symbol, r in open_rounds.items():
        logger.warning(
            "[ML标签] %s 期末未平仓回合跳过（%d 股）", symbol, r["shares"],
        )
    return pd.DataFrame(rounds)


def _entry_price_path(
    events: list, pre_close_map: dict, buy_date: str, fallback: float,
    dates: list,
) -> dict:
    """重放引擎 Holding 状态，返回 {持仓日: 当日 decision 时点 entry_price}。

    引擎口径（ret_from_entry 训练/推理同一定义契约）：
    - 买入开仓 entry=成交价；加仓 entry=cost/shares（match/manual.py）；
    - 送转 entry ×= 1/(1+stk)（corporate._apply_stk_div；trade_log 只有
      送转前后股数，以 old/new 表达）；
    - 现金分红 cost -= 税后净额，entry ×= pre_close/(pre_close+每股税前
      红利)（corporate._apply_cash_div）；红利税按 (除息日-entry_date)
      日历天数分档 ≤30 天 20% / ≤1 年 10% / 其余免征，从净额反解税前值。
    无 events（手工构造的 pairs_df）或除息日缺面板行（停牌，引擎
    scale=None 分支）时退回静态 buy_price / 不缩放，与引擎行为一致。
    """
    if not events:
        return {d: fallback for d in dates}
    cost = shares = None
    entry = fallback
    k = 0
    path = {}
    for d in dates:
        while k < len(events) and events[k][0] <= d:
            ev_date, side, price, ev_shares, net = events[k]
            if side == "BUY":
                if cost is None:
                    cost = price * ev_shares
                    shares = ev_shares
                    entry = price
                else:
                    cost += price * ev_shares
                    shares += ev_shares
                    entry = cost / shares
            elif side == "STK_DIV":
                if shares and ev_shares > 0:
                    entry *= shares / ev_shares
                shares = ev_shares
            elif side == "DIV":
                if cost is not None:
                    cost = max(0.0, cost - net)
                # 除息日自己的 pre_close；停牌缺行 → None → 不缩放，
                # 与引擎 scale=None 分支一致（不得拿恢复日 pre_close 补缩放）
                pre = pre_close_map.get(ev_date)
                if cost is not None and shares and pre is not None and pre > 0:
                    days = (
                        pd.Timestamp(ev_date) - pd.Timestamp(buy_date)
                    ).days
                    tax = 0.20 if days <= 30 else (0.10 if days <= 365 else 0.0)
                    gross_ps = net / (shares * (1.0 - tax))
                    entry *= pre / (pre + gross_ps)
            else:  # SELL
                if cost is not None and shares > 0:
                    cost *= (shares - ev_shares) / shares
                shares -= ev_shares
            k += 1
        path[d] = entry
    return path


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
            logger.warning(
                "[ML标签] %s 回合 %s → %s 仅 %d 个交易日，跳过（需 >= 3）",
                sym, pos.buy_date, pos.sell_date, len(pos_bars),
            )
            continue

        is_positive_pair = pos.trigger == "TREND_BREAK" and pos.pnl < 0
        dates = pos_bars.index.get_level_values("trade_date")
        buy_pos = date_pos.get(pos.buy_date)
        sell_pos = date_pos.get(pos.sell_date)
        entry_path = _entry_price_path(
            getattr(pos, "events", None) or [],
            dict(zip(dates, pos_bars["pre_close"]))
            if "pre_close" in pos_bars.columns else {},
            pos.buy_date, pos.buy_price, list(dates),
        )

        for i in range(len(pos_bars)):
            trade_date = dates[i]
            # 引擎在成交当日的 compute_pending 已 +1：成交日 holding_days=1。
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
            # 账户态特征重放：与引擎推理侧同一公式；entry_price 取当日
            # 引擎 Holding 状态路径（加仓/除权除息逐日重放）
            holding = Holding(
                symbol=sym, shares=100, entry_date=pos.buy_date,
                entry_price=entry_path[trade_date],
                cost=entry_path[trade_date] * 100,
                holding_days=hd,
            )
            bar = day.to_dict()
            row = {"label": label, "trade_date": trade_date}
            state = compute_state_features(spec.state_features, bar, holding)
            for name in feature_cols:
                # 与推理侧共用同一行组装（state 优先→bar 兜底→NaN）：
                # 缺失保留 NaN，trainer 在 scaler 之后填 0（训练段均值）
                row[name], _ = assemble_feature_value(state, bar, name)
            samples.append(row)

    return pd.DataFrame(samples)
