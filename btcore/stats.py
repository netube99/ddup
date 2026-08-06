import logging
import math
from collections import defaultdict, deque

import numpy as np
import pandas as pd

from btcore.constants import CAL_DAYS_ANNUAL, TRADE_EVENT_PRIORITY

logger = logging.getLogger(__name__)


def calculate_statistics(
    account_daily_df: pd.DataFrame,
    trade_log_df: pd.DataFrame,
    benchmark_df: pd.DataFrame | None = None,
    risk_free_rate: float = 0.02,
    annual_days: int = CAL_DAYS_ANNUAL,
    holdings: dict | None = None,
) -> dict:
    """全量统计指标。

    键约定（CONS-03 / EDGE-17 双口径说明）：
    - total_dividend_received: 仅已实现 trip 分红（旧口径，保持不变）；
      total_dividend_accrued: 已实现 trip 分红 + 期末未平仓 lot 的
      dividend_accrued（含未实现分红的全口径）。
    - max_drawdown_recovery_days: 回撤未修复时保留旧语义 len(after_dd)（含谷值日，
      消费方 research/report.py 的 _int 无法渲染 None，故不动旧键值）；
      此时新增键 max_dd_unrecovered=True 显式标记。
    """
    result: dict = {}

    if account_daily_df.empty:
        result["error"] = "account_daily 为空, 无法计算统计指标"
        return result

    total_values = account_daily_df["total_value"].values
    initial_capital = account_daily_df["initial_capital"].iloc[0]

    # np.divide + where= 避免两分支先求值产生除零警告;
    # out=1.0 使被掩码位置减 1 后恰为 0.0, 与原 np.where 口径一致
    daily_returns = np.divide(
        total_values[1:], total_values[:-1],
        out=np.ones(len(total_values) - 1),
        where=total_values[:-1] > 0,
    ) - 1.0
    daily_returns = np.insert(daily_returns, 0, 0.0)

    n_days = len(total_values)
    avg_total_value = np.mean(total_values)
    total_return = (total_values[-1] / initial_capital - 1) if initial_capital > 0 else 0.0

    n_years = n_days / annual_days if n_days > 0 else 1.0
    annualized_return = (1.0 + total_return) ** (1.0 / n_years) - 1.0 if n_years > 0 else 0.0

    result["total_return"] = total_return
    result["annualized_return"] = annualized_return
    result["initial_capital"] = initial_capital
    result["final_value"] = total_values[-1]
    result["total_days"] = n_days

    dates = pd.to_datetime(account_daily_df["date"], format="%Y%m%d")
    result["start_date"] = dates.iloc[0].strftime("%Y-%m-%d")
    result["end_date"] = dates.iloc[-1].strftime("%Y-%m-%d")

    result["monthly_returns"] = _compute_period_returns(dates, total_values, "M")
    result["yearly_returns"] = _compute_period_returns(dates, total_values, "Y")

    rets = daily_returns[1:] if len(daily_returns) > 1 else np.array([])
    volatility = np.std(rets, ddof=1) if len(rets) > 1 else 0.0
    annualized_vol = volatility * np.sqrt(annual_days)
    result["annualized_volatility"] = annualized_vol

    rf_daily = risk_free_rate / annual_days if annual_days > 0 else 0.0
    excess_rets = rets - rf_daily
    mean_excess = np.mean(excess_rets) if len(excess_rets) > 0 else 0.0
    result["sharpe"] = (mean_excess / volatility * np.sqrt(annual_days)) if volatility > 0 else 0.0

    downside_rets = rets[rets < 0]
    downside_vol = np.std(downside_rets, ddof=1) if len(downside_rets) > 1 else 0.0
    result["sortino"] = (
        (mean_excess / downside_vol * np.sqrt(annual_days)) if downside_vol > 0 else 0.0
    )

    peak = total_values[0]
    max_dd = 0.0
    max_dd_start = 0
    max_dd_end = 0
    current_start = 0
    dd_recovery_days = 0
    for i, v in enumerate(total_values):
        if v > peak:
            peak = v
            current_start = i
        dd = (peak - v) / peak if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_start = current_start
            max_dd_end = i
    max_dd_unrecovered = False
    if max_dd_end > max_dd_start:
        peak_val = total_values[max_dd_start]
        after_dd = total_values[max_dd_end:]
        recovered = after_dd >= peak_val
        if np.any(recovered):
            dd_recovery_days = int(np.argmax(recovered))
        else:
            # 未修复：保留旧值 len(after_dd)（消费方 _int 无法渲染 None），
            # 新增 max_dd_unrecovered 显式标记（EDGE-17）
            dd_recovery_days = len(after_dd)
            max_dd_unrecovered = True
    result["max_drawdown"] = max_dd
    result["max_drawdown_recovery_days"] = dd_recovery_days
    result["max_dd_unrecovered"] = max_dd_unrecovered
    result["calmar"] = (annualized_return / max_dd) if max_dd > 0 else 0.0

    profit_days = int(np.sum(rets > 0))
    loss_days = int(np.sum(rets < 0))
    total_trade_days = profit_days + loss_days
    result["profit_days"] = profit_days
    result["loss_days"] = loss_days
    result["win_rate"] = profit_days / total_trade_days if total_trade_days > 0 else 0.0

    if not trade_log_df.empty:
        trades = trade_log_df[trade_log_df["side"] != "DIV"]
        div_log = trade_log_df[trade_log_df["side"] == "DIV"]
    else:
        trades = trade_log_df
        div_log = trade_log_df

    # 真实成交行（BUY/SELL）：STK_DIV/ADJUST 公司行为行不计入笔数与标的数
    exec_trades = trades[trades["side"].isin(["BUY", "SELL"])]

    if not trades.empty:
        buy_count = int((trades["side"] == "BUY").sum())
        sell_count = int((trades["side"] == "SELL").sum())
        result["trade_count"] = len(exec_trades)
        result["buy_count"] = buy_count
        result["sell_count"] = sell_count
        result["unique_symbols"] = int(exec_trades["symbol"].nunique())
        result["daily_avg_turnover"] = trades["turnover"].sum() / n_days if n_days > 0 else 0.0

        avg_pos = _avg_positions_from_account_daily(account_daily_df)
        if avg_pos is None:
            act_days = trades["date"].nunique()
            result["avg_positions"] = (
                trades.groupby("date")["symbol"].nunique().mean() if act_days > 0 else 0.0
            )
        else:
            result["avg_positions"] = avg_pos

        total_turnover = trades["turnover"].sum()
        result["turnover_rate"] = (total_turnover / avg_total_value) if avg_total_value > 0 else 0.0
    else:
        result["trade_count"] = 0
        result["buy_count"] = 0
        result["sell_count"] = 0
        result["unique_symbols"] = 0
        result["daily_avg_turnover"] = 0.0
        avg_pos = _avg_positions_from_account_daily(account_daily_df)
        result["avg_positions"] = avg_pos if avg_pos is not None else 0.0
        result["turnover_rate"] = 0.0

    trip_result = _compute_round_trips(trades, div_log, account_daily_df, holdings)
    result.update(trip_result)
    result.update(_compute_sell_source(result["round_trip"]["trip_detail"]))

    result.update(_compute_symbol_contribution(trades, div_log, holdings))
    result.update(_compute_cost_breakdown(trades))
    result.update(
        _compute_trading_friction(
            trades, result["cost_breakdown"],
            result["round_trip"]["trip_detail"], annual_days,
            total_values, initial_capital, n_days, avg_total_value,
        )
    )
    result.update(_compute_management_complexity(trades, account_daily_df))

    result["benchmark_compare"] = _compute_benchmark_compare(
        account_daily_df, benchmark_df, annual_days, risk_free_rate
    )

    return result


def _avg_positions_from_account_daily(adf: pd.DataFrame) -> float | None:
    if "n_holdings" not in adf.columns:
        return None
    col = adf["n_holdings"]
    if len(col) == 0:
        return None
    return float(col.astype(float).mean())


def _compute_period_returns(dates: pd.DatetimeIndex, total_values: np.ndarray,
                            freq: str) -> dict[str, float]:
    """按频率取各期期末值计算区间收益（"M"=月度, "Y"=年度），键为期标签。

    月度键为 YYYY-MM（str(Period)），年度键为年份字符串，首期为基期不计收益。
    """
    if len(total_values) < 2:
        return {}
    work = pd.DataFrame({"dt": dates, "tv": total_values})
    period_end = work.groupby(work["dt"].dt.to_period(freq)).last()
    if len(period_end) < 2:
        return {}
    vals = period_end["tv"].values
    rets = vals[1:] / vals[:-1] - 1.0
    labels = [str(p) for p in period_end.index[1:]]
    return dict(zip(labels, rets))


def _compute_round_trips(trades: pd.DataFrame, div_log: pd.DataFrame,
                         account_daily_df: pd.DataFrame,
                         holdings: dict | None = None) -> dict:
    """全事件流时序 FIFO（CONS-01：pnl 统一为含费用口径）。

    BUY / SELL / DIV 按日期排序后逐日处理：
    - BUY: 创建新 lot 入队 (shares, cost_total=abs(net_amount 含费用),
      buy_date, dividend_accrued=0)
    - DIV: 按除息日当前各 lot 持股比例分发红利到 lot.dividend_accrued
    - SELL: FIFO 弹 lot，买入成本与卖出净收入均按比例分摊 net_amount
      （含费用），每个 lot 自带分红，卖出时一并结算

    pnl = 卖出净额(含费) - 买入成本(含费) + 已分配分红——与
    symbol_contribution / ML 标签同一净额口径（三处不再各自为政）。

    修复 L1（多次部分卖出时分红基数未扣减已售份额）和
    L2（除息日后买入仍被分配分红）两个 bug。
    """
    if trades.empty and div_log.empty:
        return {"round_trip": {"trip_detail": [], "open_positions": [], "summary": {}}}

    # 构建统一事件流: BUY / SELL / DIV / STK_DIV 四类, 按日期排序。
    # CONS-01：BUY/SELL 携带 net_amount（含费用净额，BUY 为负）——往返盈亏
    # 统一为含费用口径（与 symbol_contribution / ML 标签一致）。
    events = []
    for _, row in trades.iterrows():
        side = row["side"]
        price = float(row["price"])
        shares = int(row["shares"])
        net = row.get("net_amount")
        if net is None or (isinstance(net, float) and math.isnan(net)):
            # 防御：非引擎构造的 trade_log 缺 net_amount 时回退裸价口径
            # （引擎/真实库 schema 必含 net_amount，此路径不应出现）
            logger.warning(
                "trade_log 缺 net_amount，回退裸价口径（%s %s %s）",
                row["date"], row["symbol"], side,
            )
            net = (price * shares) if side == "SELL" else -(price * shares)
        events.append({
            "date": row["date"], "symbol": row["symbol"], "type": side,
            "shares": shares, "price": price, "net_amount": float(net),
            "trigger": row.get("trigger", "") or "",
        })
    for _, row in div_log.iterrows():
        events.append({
            "date": row["date"], "symbol": row["symbol"], "type": "DIV",
            "net_amount": abs(row["net_amount"]),
        })
    # 同日事件按 A 股时序：除息/送转（盘前）→ 买入 → 卖出。
    # 旧排序仅按 date（同日 SELL 先于 DIV），除息日清仓的持仓分红整体丢失
    # （2026-08-03 实证：champ 全周期 total_dividend_received 低估 -63%）。
    # ADJUST 行（live 衍生库，shares=0）同为盘前优先级，下方事件循环无匹配
    # 分支自然跳过，不动 lot，无害。
    events.sort(key=lambda e: (e["date"], TRADE_EVENT_PRIORITY.get(e["type"], 3)))

    # 每只股票维护 lot 队列: [{shares, cost_per_share, buy_date, dividend_accrued}]
    lots: dict[str, deque] = defaultdict(deque)
    trip_detail = []

    for event in events:
        sym = event["symbol"]
        if event["type"] == "BUY":
            # cost_total 含费用（abs(net_amount)）；部分卖出按比例分摊，
            # 送转缩放时总成本不变（每股成本自动下摊）
            lots[sym].append({
                "shares": event["shares"],
                "cost_total": abs(event["net_amount"]),
                "buy_date": event["date"],
                "dividend_accrued": 0.0,
            })
        elif event["type"] == "DIV":
            div_amount = event["net_amount"]
            if div_amount <= 0:
                continue
            total_open = sum(lot["shares"] for lot in lots[sym])
            if total_open > 0:
                per_share_div = div_amount / total_open
                for lot in lots[sym]:
                    lot["dividend_accrued"] += lot["shares"] * per_share_div
        elif event["type"] == "STK_DIV":
            # 送转增股：按比例缩放 lot 队列（shares × ratio，总成本不变），
            # 除权后卖出才能与引擎口径对得上
            total_open = sum(lot["shares"] for lot in lots[sym])
            if total_open <= 0:
                continue
            ratio = event["shares"] / total_open
            new_total = 0
            for lot in lots[sym]:
                scaled = int(lot["shares"] * ratio)
                lot["shares"] = scaled
                new_total += scaled
            if new_total < event["shares"]:
                lots[sym][0]["shares"] += event["shares"] - new_total
        elif event["type"] == "SELL":
            sell_shares = event["shares"]
            sell_date = event["date"]

            remaining = sell_shares
            while remaining > 0 and lots[sym]:
                lot = lots[sym][0]
                matched = min(remaining, lot["shares"])

                # CONS-01 含费用口径：买入成本按比例分摊买入净额（含费），
                # 卖出净收入按比例分摊卖出净额（含费），分红不受费用影响
                matched_cost = lot["cost_total"] * (matched / lot["shares"])
                sell_net = event["net_amount"] * (matched / sell_shares)
                matched_div = (
                    lot["dividend_accrued"] * (matched / lot["shares"])
                    if lot["shares"] > 0 else 0.0
                )

                pnl = sell_net - matched_cost + matched_div
                pnl_pct = pnl / matched_cost if matched_cost > 0 else 0.0

                hold_start = pd.Timestamp(lot["buy_date"])
                hold_end = pd.Timestamp(sell_date)
                holding_days = (hold_end - hold_start).days

                trip_detail.append({
                    "symbol": sym,
                    "entry_date": lot["buy_date"],
                    "exit_date": sell_date,
                    "shares": int(matched),
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "holding_days": holding_days,
                    "dividend_received": matched_div,
                    "sell_trigger": event["trigger"],
                })

                if matched >= lot["shares"]:
                    lots[sym].popleft()
                else:
                    lot["shares"] -= matched
                    lot["cost_total"] -= matched_cost
                    lot["dividend_accrued"] -= matched_div
                remaining -= matched

            if remaining > 0:
                # CONS-04：SELL 超出未平仓 lot 股数时 realized pnl 静默少算，
                # 不 raise（兼容脏数据），但必须告警
                logger.warning(
                    "SELL 超出未平仓 lot：%s %s 卖出 %d 股，其中 %d 股无 lot 匹配"
                    "（realized pnl 将少算，请检查 trade_log 数据完整性）",
                    sell_date, sym, sell_shares, remaining,
                )

    # 剩余 lots = 未平仓持仓
    open_positions = []
    total_open_cost = 0.0
    final_prices = _build_final_price_map(holdings)

    for sym, sym_lots in lots.items():
        for lot in sym_lots:
            # cost_basis 含费用（CONS-01，与已实现口径一致）
            cost_basis = lot["cost_total"]
            total_open_cost += cost_basis

            last_price = final_prices.get(
                sym,
                lot["cost_total"] / lot["shares"] if lot["shares"] > 0 else 0.0,
            )
            open_pnl = lot["shares"] * last_price - cost_basis + lot["dividend_accrued"]

            hold_start = pd.Timestamp(lot["buy_date"])
            hold_end = pd.Timestamp(account_daily_df["date"].iloc[-1])
            holding_days = (hold_end - hold_start).days

            open_positions.append({
                "symbol": sym,
                "entry_date": lot["buy_date"],
                "exit_date": None,
                "shares": int(lot["shares"]),
                "pnl": open_pnl,
                "pnl_pct": open_pnl / cost_basis if cost_basis > 0 else 0.0,
                "holding_days": holding_days,
                "dividend_received": lot["dividend_accrued"],
                "cost_basis": cost_basis,
            })

    # 汇总
    total_realized_pnl = sum(t["pnl"] for t in trip_detail)
    total_unrealized_pnl = sum(p["pnl"] for p in open_positions)

    # trip_detail 全部带 exit_date（未平仓的在 open_positions）
    completed_trips = trip_detail
    win_trips = [t for t in completed_trips if t["pnl"] > 0]
    loss_trips = [t for t in completed_trips if t["pnl"] < 0]

    summary = {
        "total_round_trips": len(completed_trips),
        "open_positions": len(open_positions),
        "total_realized_pnl": total_realized_pnl,
        "total_unrealized_pnl": total_unrealized_pnl,
        "win_count": len(win_trips),
        "loss_count": len(loss_trips),
        "win_loss_ratio": len(win_trips) / len(loss_trips) if len(loss_trips) > 0 else 0.0,
        # 双口径（CONS-03）：旧键 total_dividend_received 只计已实现 trip 分红，
        # 保持不变；新键 total_dividend_accrued 追加期末未平仓 lot 的
        # dividend_accrued（未实现分红不再静默低估）
        "total_dividend_received": sum(t["dividend_received"] for t in trip_detail),
        "total_dividend_accrued": (
            sum(t["dividend_received"] for t in trip_detail)
            + sum(p["dividend_received"] for p in open_positions)
        ),
        "avg_pnl": np.mean([t["pnl"] for t in completed_trips]) if completed_trips else 0.0,
        "avg_holding_days": (
            np.mean([t["holding_days"] for t in completed_trips]) if completed_trips else 0.0
        ),
    }

    return {
        "round_trip": {
            "trip_detail": trip_detail,
            "open_positions": open_positions,
            "summary": summary,
        }
    }


def _compute_sell_source(trip_detail: list) -> dict:
    """按卖出 trigger 分组的 round-trip 归因（信号卖/调仓/条件单）。"""
    if not trip_detail:
        return {"sell_source": {}}
    groups: dict[str, list] = defaultdict(list)
    for t in trip_detail:
        groups[t.get("sell_trigger") or "UNKNOWN"].append(t)
    out = {}
    for trigger, trips in sorted(groups.items()):
        pnls = [t["pnl"] for t in trips]
        out[trigger] = {
            "count": len(trips),
            "total_pnl": float(sum(pnls)),
            "avg_pnl": float(np.mean(pnls)),
            "win_rate": float(np.mean([p > 0 for p in pnls])),
            "avg_holding_days": float(np.mean([t["holding_days"] for t in trips])),
        }
    return {"sell_source": out}


def _build_final_price_map(holdings: dict | None) -> dict[str, float]:
    """从期末 holdings 中提取各 symbol 的 last_price。"""
    if not holdings:
        return {}
    result = {}
    for sym, h in holdings.items():
        lp = getattr(h, "last_price", None)
        if lp is not None and lp > 0:
            result[sym] = float(lp)
    return result


def _compute_symbol_contribution(trades: pd.DataFrame, div_log: pd.DataFrame,
                                 holdings: dict | None = None) -> dict:
    if trades.empty:
        return {"symbol_contribution": {}}

    # PERF-02：一次性 groupby 查表替代循环内全表布尔过滤（O(S×D) → O(S)）
    div_by_symbol = (
        div_log.groupby("symbol")["net_amount"].sum() if not div_log.empty else None
    )

    contribution = {}
    for sym, grp in trades.groupby("symbol"):
        buy_rows = grp[grp["side"] == "BUY"]
        sell_rows = grp[grp["side"] == "SELL"]
        realized_pnl = sell_rows["net_amount"].sum() + buy_rows["net_amount"].sum()
        div_amount = (
            div_by_symbol.get(sym, 0.0) if div_by_symbol is not None else 0.0
        )

        unrealized_pnl = 0.0
        if holdings and sym in holdings:
            h = holdings[sym]
            shares = getattr(h, "shares", 0)
            last_price = getattr(h, "last_price", 0.0)
            cost = getattr(h, "cost", 0.0)
            if shares > 0:
                unrealized_pnl = shares * last_price - cost

        contribution[sym] = {
            "realized_pnl": realized_pnl,
            "unrealized_pnl": unrealized_pnl,
            "dividend_received": abs(div_amount),
            "total_contribution": realized_pnl + unrealized_pnl + abs(div_amount),
        }

    return {"symbol_contribution": contribution}


def _compute_cost_breakdown(trades: pd.DataFrame) -> dict:
    if trades.empty:
        return {"cost_breakdown": {}}

    buy_trades = trades[trades["side"] == "BUY"]
    sell_trades = trades[trades["side"] == "SELL"]

    breakdown = {
        "buy_commission": buy_trades["commission"].sum(),
        "sell_commission": sell_trades["commission"].sum(),
        "stamp_tax": sell_trades["stamp_tax"].sum(),
        "transfer_fee": trades["transfer_fee"].sum(),
        "slippage": trades["slippage_amount"].sum(),
        "total_cost": (trades["commission"].sum() + sell_trades["stamp_tax"].sum()
                       + trades["transfer_fee"].sum() + trades["slippage_amount"].sum()),
    }
    return {"cost_breakdown": breakdown}


def _compute_trading_friction(trades: pd.DataFrame, cost_breakdown: dict,
                              trip_detail: list, annual_days: int,
                              total_values: np.ndarray, initial_capital: float,
                              n_days: int, avg_total_value: float) -> dict:
    """散户视角的交易磨损：费用/滑点对收益的侵蚀程度。

    HYG-06：total_values/initial_capital/n_days/avg_total_value 由
    calculate_statistics 一次性算好传入（n_years 由 n_days 派生，口径相同），
    消除两处重复计算与潜在漂移。
    """
    zero = {
        "total_cost": 0.0,
        "cost_per_trade": 0.0,
        "cost_pct_of_turnover": 0.0,
        "annualized_cost_drag": 0.0,
        "cost_pct_of_gross_profit": 0.0,
        "no_cost_total_return": 0.0,
        "slippage_share": 0.0,
    }
    if trades.empty or not cost_breakdown:
        return {"trading_friction": zero}

    total_cost = float(cost_breakdown["total_cost"])
    n_years = n_days / annual_days if n_days > 0 else 1.0

    total_turnover = float(trades["turnover"].sum())
    gross_profit = sum(t["pnl"] for t in trip_detail if t["pnl"] > 0)

    friction = {
        "total_cost": total_cost,
        "cost_per_trade": total_cost / len(trades) if len(trades) > 0 else 0.0,
        "cost_pct_of_turnover": total_cost / total_turnover if total_turnover > 0 else 0.0,
        "annualized_cost_drag": (
            (total_cost / avg_total_value) / n_years
            if avg_total_value > 0 and n_years > 0 else 0.0
        ),
        "cost_pct_of_gross_profit": total_cost / gross_profit if gross_profit > 0 else 0.0,
        "no_cost_total_return": (
            (total_values[-1] + total_cost) / initial_capital - 1.0
            if initial_capital > 0 else 0.0
        ),
        "slippage_share": (
            float(cost_breakdown["slippage"]) / total_cost if total_cost > 0 else 0.0
        ),
    }
    return {"trading_friction": friction}


def _compute_management_complexity(trades: pd.DataFrame,
                                   account_daily_df: pd.DataFrame) -> dict:
    """散户视角的持仓管理复杂度：手动跟单的操作负担与资金门槛。"""
    zero = {
        "max_positions": 0,
        "avg_trades_per_day": 0.0,
        "avg_trades_per_active_day": 0.0,
        "max_trades_per_day": 0,
        "active_day_ratio": 0.0,
        "avg_buy_amount": 0.0,
        "min_buy_amount": 0.0,
        "avg_position_value": 0.0,
    }
    n_days = len(account_daily_df)
    if n_days == 0:
        return {"management_complexity": zero}

    if "n_holdings" in account_daily_df.columns:
        nh = account_daily_df["n_holdings"].astype(float)
        max_positions = int(nh.max())
        held = account_daily_df[nh > 0]
        avg_position_value = (
            float((held["total_value"] / nh[nh > 0]).mean()) if len(held) > 0 else 0.0
        )
    else:
        max_positions = 0
        avg_position_value = 0.0

    if trades.empty:
        complexity = {
            **zero,
            "max_positions": max_positions,
            "avg_position_value": avg_position_value,
        }
        return {"management_complexity": complexity}

    active_days = int(trades["date"].nunique())
    buys = trades[trades["side"] == "BUY"]["turnover"]

    complexity = {
        "max_positions": max_positions,
        "avg_trades_per_day": len(trades) / n_days,
        "avg_trades_per_active_day": len(trades) / active_days if active_days > 0 else 0.0,
        "max_trades_per_day": int(trades.groupby("date").size().max()),
        "active_day_ratio": active_days / n_days,
        "avg_buy_amount": float(buys.mean()) if len(buys) > 0 else 0.0,
        "min_buy_amount": float(buys.min()) if len(buys) > 0 else 0.0,
        "avg_position_value": avg_position_value,
    }
    return {"management_complexity": complexity}


def _compute_benchmark_compare(account_daily_df: pd.DataFrame,
                               benchmark_df: pd.DataFrame | None,
                               annual_days: int,
                               risk_free_rate: float) -> dict:
    if benchmark_df is None or benchmark_df.empty:
        return {}

    adf = account_daily_df.set_index("date").sort_index()
    bdf = benchmark_df.copy()

    if "date" in bdf.columns:
        bdf = bdf.set_index("date")
    bdf = bdf.sort_index()

    bdf.index = pd.to_datetime(bdf.index).strftime("%Y%m%d")

    common_dates = adf.index.intersection(bdf.index)
    if len(common_dates) < 2:
        return {}

    strat_vals = adf.loc[common_dates, "total_value"].values
    bench_vals = bdf.loc[common_dates].iloc[:, 0].values

    strat_rets = strat_vals[1:] / strat_vals[:-1] - 1.0
    bench_rets = bench_vals[1:] / bench_vals[:-1] - 1.0

    rf_daily = risk_free_rate / annual_days if annual_days > 0 else 0.0

    # TEST-07：len<2 时 np.var/np.std(ddof=1) 触发 "Degrees of freedom <= 0"
    # RuntimeWarning（2 日窗口仅 1 个收益样本）；长度守卫输出 NaN 而非告警。
    # 数值语义与原路径一致（nan > 0 为 False → beta/ir 仍走 0.0 分支）。
    bench_var = float(np.var(bench_rets, ddof=1)) if len(bench_rets) > 1 else float("nan")
    if bench_var > 0:
        beta = float(np.cov(strat_rets, bench_rets, ddof=1)[0, 1] / bench_var)
    else:
        beta = 0.0

    excess = strat_rets - bench_rets
    excess_std = float(np.std(excess, ddof=1)) if len(excess) > 1 else float("nan")
    tracking_error = excess_std * np.sqrt(annual_days)
    ir = (
        float(np.mean(excess) / excess_std * np.sqrt(annual_days))
        if excess_std > 0 else 0.0
    )

    strat_mean = np.mean(strat_rets) if len(strat_rets) > 0 else 0.0
    bench_mean = np.mean(bench_rets) if len(bench_rets) > 0 else 0.0
    alpha = (strat_mean - rf_daily) - beta * (bench_mean - rf_daily)
    alpha_annualized = alpha * annual_days

    bench_total_return = bench_vals[-1] / bench_vals[0] - 1.0 if bench_vals[0] > 0 else 0.0
    strat_total_return = strat_vals[-1] / strat_vals[0] - 1.0 if strat_vals[0] > 0 else 0.0

    # 基准年化收益
    n_years = (len(bench_vals) - 1) / annual_days if annual_days > 0 else 0
    bench_ann = (1 + bench_total_return) ** (1 / n_years) - 1 if n_years > 0 else 0.0

    # 基准最大回撤
    bench_nav = bench_vals / bench_vals[0]
    bench_peak = np.maximum.accumulate(bench_nav)
    bench_dd = float(np.min(bench_nav / bench_peak - 1.0))

    return {
        "alpha": alpha_annualized,
        "beta": beta,
        "information_ratio": ir,
        "tracking_error": tracking_error,
        "benchmark_total_return": bench_total_return,
        "benchmark_annual_return": bench_ann,
        "benchmark_max_drawdown": bench_dd,
        "strategy_total_return": float(strat_total_return),
    }
