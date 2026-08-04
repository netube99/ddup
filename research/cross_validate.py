"""Cross-Validation — 回测结果交叉验证（可 import 库）。

检查回测结果与策略设计意图的一致性：
1. 交易行为验证（买卖是否在预期范围内）
2. 异常检测（过度交易、异常滑点等）
3. 小资金专项检查（交易磨损占比合理性）

scripts/cross_validate.py 是薄壳 CLI；被测逻辑全部在本模块。
注：涨停买入由引擎自动跳过，无法从 trade_log 判定，不做该项检查。
"""

import json
import sqlite3
from collections import Counter

from btcore import database
from btcore.constants import MIN_COMMISSION, STAMP_TAX_RATE
from btcore.match import conditions as condition_registry
from btcore.ml import conditions as ml_conditions
from research.cli_common import latest_run_id

# 引擎直写 trade_log 的固定 trigger（不经条件单注册表）
_ENGINE_TRIGGERS = {"MANUAL", "TARGET", "CORPORATE"}


def _expected_triggers() -> set[str]:
    """预期 trigger = 引擎固定集 ∪ 条件单注册表（含自定义 handler）。

    ml_conditions.register() 幂等，保证 ML_EXIT 在未走 strategy_loader 的
    独立进程里也在预期集合内。
    """
    ml_conditions.register()
    return _ENGINE_TRIGGERS | condition_registry.registered_condition_types()


def load_backtest(db_path: str, run_id: int | None = None) -> tuple:
    """加载回测结果。返回 (trades_df, daily_df, stats_dict, config, run)。

    读库走 database.init_backtest_db + read_run_data（trade_log 按
    date,id 排序，保证同日时序与写入序一致）。
    """
    conn = database.init_backtest_db(db_path)
    conn.row_factory = sqlite3.Row

    if run_id is None:
        run_id = latest_run_id(conn)
        if run_id is None:
            conn.close()
            raise ValueError("No runs found in database")

    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        conn.close()
        raise ValueError(f"Run {run_id} not found")

    daily, trades, stats = database.read_run_data(conn, run_id)
    stats = stats or {}
    config = json.loads(run["config_json"]) if run["config_json"] else {}

    conn.close()
    return trades, daily, stats, config, dict(run)


def _min_commission_overhead(n_buys: int, capital: float, min_commission: float) -> float:
    """最低佣金导致的固定成本占比。每笔最低 min_commission 元。"""
    if capital <= 0:
        return 0.0
    return (n_buys * min_commission) / capital


def validate_trades(trades, config, strategy_name="", capital: float = 0):
    """验证交易记录与策略设计的一致性。"""
    issues = []
    notes = []

    # 0. 基本统计
    n_buys = len(trades[trades["side"] == "BUY"])
    n_sells = len(trades[trades["side"] == "SELL"])
    n_days = trades["date"].nunique()
    symbols_traded = trades["symbol"].nunique()
    total_turnover = trades["turnover"].sum()
    total_commission = trades["commission"].sum()
    total_stamp = trades["stamp_tax"].sum()
    total_transfer = trades["transfer_fee"].sum()
    total_slippage = trades["slippage_amount"].sum()
    # 与 btcore/stats.py cost_breakdown.total_cost 同口径：
    # 佣金 + 印花税 + 过户费 + 滑点
    total_costs = total_commission + total_stamp + total_transfer + total_slippage

    notes.append(f"交易天数: {n_days}, 总成交: {len(trades)} 笔")
    notes.append(f"买入 {n_buys} 笔, 卖出 {n_sells} 笔, 涉及 {symbols_traded} 只票")
    notes.append(f"总成交额: {total_turnover:,.0f} 元")
    notes.append(f"总费用: {total_costs:,.1f} 元 (佣金 {total_commission:,.1f}, "
                 f"印花税 {total_stamp:,.1f}, 过户费 {total_transfer:,.1f}, "
                 f"滑点 {total_slippage:,.1f})")

    # 1. 检查交易触发类型分布
    trigger_dist = Counter(trades["trigger"].dropna())
    notes.append(f"触发类型分布: {dict(trigger_dist)}")

    # 预期集合 = 引擎固定 trigger ∪ 条件单注册表（自定义 handler 是
    # 一等公民，condition_hunter/multi_model 的 DYNAMIC_STOP 等不应误报）
    unexpected = set(trigger_dist) - _expected_triggers()
    if unexpected:
        notes.append(
            f"非内置触发类型（自定义 handler，按 INFO 处理）: {sorted(unexpected)}"
        )

    # 2. 检查买卖比例平衡
    if n_buys > 0 and n_sells > 0:
        ratio = n_sells / n_buys
        if ratio > 3:
            issues.append(f"HIGH_SELL_RATIO: 卖出/买入比 = {ratio:.1f} (预期 ~1.0)")
        elif ratio < 0.3:
            issues.append(f"LOW_SELL_RATIO: 卖出/买入比 = {ratio:.1f}")

    # 3. 检查是否有同日买卖冲突
    t = trades.copy()
    t["key"] = t["date"] + "_" + t["symbol"]
    dup = t.groupby("key").filter(lambda g: len(g) > 1)
    if len(dup) > 0:
        day_sym_conflicts = dup.groupby(["date", "symbol"]).agg(
            sides=("side", lambda x: set(x))
        ).reset_index()
        conflicts = day_sym_conflicts[day_sym_conflicts["sides"].apply(
            lambda s: "BUY" in s and "SELL" in s
        )]
        if len(conflicts) > 0:
            issues.append(f"SAME_DAY_CONFLICT: {len(conflicts)} 个同日买卖冲突: "
                          f"{conflicts[['date', 'symbol']].to_dict('records')[:5]}")

    # 4. 检查公司行为
    corp = trades[trades["trigger"] == "CORPORATE"]
    if len(corp) > 0:
        notes.append(f"CORPORATE: {len(corp)} 笔公司行为（分红/送转）")

    # 5. 小资金专项：交易磨损占比（动态阈值 = 最低佣金开销 + 2%）
    # 成本口径从 run 的 config 读取（引擎费率可配置，硬编码 5 元/0.05% 会失真）
    min_commission = float(config.get("min_commission", MIN_COMMISSION))
    stamp_min = float(config.get("stamp_tax_rate", STAMP_TAX_RATE))
    capital = capital or config.get("initial_capital", 0) or 1_000_000
    if capital > 0:
        cost_ratio = total_costs / capital
        min_overhead = _min_commission_overhead(n_buys, capital, min_commission)
        # 资本感知阈值：不可避免部分（双向最低佣金 + 印花税底） + 按资金规模的可变上限
        _stamp_min = stamp_min  # 卖出印花税底
        if capital <= 50000:
            _variable = 0.03
        elif capital <= 500000:
            _variable = 0.01
        else:
            _variable = 0.005
        threshold = min_overhead * 2 + _stamp_min + _variable
        notes.append(f"交易磨损/资金比: {cost_ratio:.4%} (阈值 {threshold:.2%}, "
                     f"最低佣金开销 {min_overhead:.2%})")
        if cost_ratio > threshold:
            msg = f"HIGH_COST_RATIO: 交易磨损 {cost_ratio:.2%} > 阈值 {threshold:.2%}"
            if capital <= 50000:
                notes.append(msg)
            else:
                issues.append(msg)

    # 6. 检查单笔交易金额合理性
    buy_trades = trades[trades["side"] == "BUY"]
    if len(buy_trades) > 0:
        avg_buy = buy_trades["turnover"].mean()
        min_buy = buy_trades["turnover"].min()
        notes.append(f"买入均值: {avg_buy:,.0f} 元, 最小: {min_buy:,.0f} 元")
        small_buys = buy_trades[buy_trades["turnover"] < 25000]
        if len(small_buys) > 0:
            pct = len(small_buys) / len(buy_trades)
            notes.append(f"小单买入: {len(small_buys)}/{len(buy_trades)} ({pct:.1%}) "
                         f"触发最低佣金 5 元")
            # 小资金（<10万）必然触发，不报警
            if capital >= 100000 and pct > 0.5:
                issues.append(f"TOO_MANY_SMALL_TRADES: {pct:.1%} 的买入触发最低佣金")

    # 7. 检查持有周期
    sell_trades = trades[trades["side"] == "SELL"]
    if len(sell_trades) > 0:
        sell_trigger_stats = sell_trades.groupby("trigger").agg(
            count=("turnover", "count"),
            avg_amount=("turnover", "mean"),
            total_pnl=("net_amount", "sum"),
        )
        notes.append(f"卖出分类统计:\n{sell_trigger_stats.to_string()}")

    # 8. 交易频率检查
    avg_trades_per_day = len(trades) / n_days if n_days > 0 else 0
    notes.append(f"日均交易: {avg_trades_per_day:.1f} 笔")
    if avg_trades_per_day > 10:
        issues.append(f"HIGH_FREQ: 日均交易 {avg_trades_per_day:.1f} 笔（过高）")

    return issues, notes


def validate_daily(daily, config):
    """验证每日账户数据。"""
    issues = []
    notes = []

    if len(daily) == 0:
        return issues, notes

    # 期初资金检查（兜底与引擎默认一致，engine.py:102）
    capital = config.get("initial_capital", 0) or 1_000_000
    init_total = daily.iloc[0]["total_value"]
    if abs(init_total - capital) > 1:
        notes.append(f"期初权益: {init_total:,.0f} (设定 {capital:,.0f})")

    # 终值
    final_total = daily.iloc[-1]["total_value"]
    final_pnl = final_total - capital
    total_return = final_pnl / capital
    notes.append(f"终期权益: {final_total:,.0f}, 总收益: {final_pnl:,.0f} ({total_return:.2%})")

    # 现金非负
    neg_cash = daily[daily["cash"] < -0.01]
    if len(neg_cash) > 0:
        issues.append(f"NEGATIVE_CASH: {len(neg_cash)} 天现金为负")

    # 持仓数检查
    max_holdings = daily["n_holdings"].max()
    max_positions = config.get("max_positions", 999)
    notes.append(f"最大持仓数: {max_holdings} (上限 {max_positions})")
    if max_holdings > max_positions:
        issues.append(f"MAX_POS_EXCEEDED: 最大持仓 {max_holdings} > 上限 {max_positions}")

    return issues, notes
