"""
Cross-Validation — 回测结果交叉验证。

检查回测结果与策略设计意图的一致性：
1. 交易行为验证（买卖是否在预期范围内）
2. 异常检测（过度交易、异常滑点等）
3. 小资金专项检查（交易磨损占比合理性）

用法：
  .venv/bin/python scripts/cross_validate.py result.db --strategy smart_money
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter

import pandas as pd

from btcore.constants import MIN_COMMISSION, STAMP_TAX_RATE
from btcore.match import conditions as condition_registry
from btcore.ml import conditions as ml_conditions

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
    """加载回测结果。返回 (trades_df, daily_df, stats_dict)。"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    if run_id is None:
        run_id = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()[0]
        if run_id is None:
            raise ValueError("No runs found in database")

    run = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    if run is None:
        raise ValueError(f"Run {run_id} not found")

    trades = pd.read_sql_query(
        "SELECT * FROM trade_log WHERE run_id = ? ORDER BY date", conn, params=(run_id,)
    )
    daily = pd.read_sql_query(
        "SELECT * FROM account_daily WHERE run_id = ? ORDER BY date", conn, params=(run_id,)
    )
    stats = json.loads(run["stats_json"]) if run["stats_json"] else {}
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
    total_slippage = trades["slippage_amount"].sum()
    total_costs = total_commission + total_stamp + total_slippage

    notes.append(f"交易天数: {n_days}, 总成交: {len(trades)} 笔")
    notes.append(f"买入 {n_buys} 笔, 卖出 {n_sells} 笔, 涉及 {symbols_traded} 只票")
    notes.append(f"总成交额: {total_turnover:,.0f} 元")
    notes.append(f"总费用: {total_costs:,.1f} 元 (佣金 {total_commission:,.1f}, "
                 f"印花税 {total_stamp:,.1f}, 滑点 {total_slippage:,.1f})")

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

    # 6. 小资金专项：交易磨损占比（动态阈值 = 最低佣金开销 + 2%）
    # 成本口径从 run 的 config 读取（引擎费率可配置，硬编码 5 元/0.05% 会失真）
    min_commission = float(config.get("min_commission", MIN_COMMISSION))
    stamp_min = float(config.get("stamp_tax_rate", STAMP_TAX_RATE))
    capital = capital or config.get("initial_capital", 0) or 40000
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

    # 5. 检查单笔交易金额合理性
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

    # 6. 检查持有周期
    sell_trades = trades[trades["side"] == "SELL"]
    if len(sell_trades) > 0:
        sell_trigger_stats = sell_trades.groupby("trigger").agg(
            count=("turnover", "count"),
            avg_amount=("turnover", "mean"),
            total_pnl=("net_amount", "sum"),
        )
        notes.append(f"卖出分类统计:\n{sell_trigger_stats.to_string()}")

    # 7. 检查是否有涨停买入（应该被引擎自动跳过，但检查一下）
    # 无法从 trade_log 直接判断，从 stats 看

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

    # 期初资金检查
    capital = config.get("initial_capital", 0) or 40000
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


def main():
    parser = argparse.ArgumentParser(description="回测结果交叉验证")
    parser.add_argument("db_path", help="结果库路径")
    parser.add_argument("--run-id", type=int, help="指定 run_id")
    parser.add_argument("--strategy", default="", help="策略名称（用于输出标注）")
    parser.add_argument("--capital", type=float, default=0,
                        help="初始资金（用于动态阈值，0=自动从 config 读取）")
    args = parser.parse_args()

    print(f"交叉验证: {args.db_path}")
    if args.strategy:
        print(f"策略: {args.strategy}")

    try:
        trades, daily, stats, config, run_info = load_backtest(args.db_path, args.run_id)
    except Exception as e:
        print(f"ERROR: 加载回测结果失败: {e}")
        return 1

    print(f"\nRun #{run_info['run_id']}: {run_info['strategy']} "
          f"({run_info['start_date']} ~ {run_info['end_date']})")

    # 验证交易
    print("\n" + "=" * 60)
    print("交易验证")
    print("=" * 60)
    trade_issues, trade_notes = validate_trades(trades, config, args.strategy, args.capital)
    for note in trade_notes:
        print(f"  [INFO] {note}")
    for issue in trade_issues:
        print(f"  [WARN] {issue}")

    # 验证账户
    print("\n" + "=" * 60)
    print("账户验证")
    print("=" * 60)
    acct_issues, acct_notes = validate_daily(daily, config)
    for note in acct_notes:
        print(f"  [INFO] {note}")
    for issue in acct_issues:
        print(f"  [WARN] {issue}")

    # 统计指标
    if stats:
        print("\n" + "=" * 60)
        print("统计指标")
        print("=" * 60)
        key_metrics = [
            ("total_return", "总收益率"),
            ("annualized_return", "年化收益率"),
            ("max_drawdown", "最大回撤"),
            ("sharpe", "夏普比率"),
            ("calmar", "Calmar 比率"),
            ("win_rate", "日胜率"),
        ]
        # 键名必须与 btcore/stats.py 实际输出对齐（历史版本用
        # annual_return/sharpe_ratio 等旧键名，静默不打印）
        for k, label in key_metrics:
            if k in stats:
                v = stats[k]
                if isinstance(v, float):
                    print(f"  {label}: {v:.4f}")
                else:
                    print(f"  {label}: {v}")
        avg_holding = stats.get("round_trip", {}).get("summary", {}).get("avg_holding_days")
        if avg_holding is not None:
            print(f"  平均持有天数: {float(avg_holding):.2f}")

        # 交易磨损
        friction = stats.get("trading_friction", {})
        if friction:
            print("\n  交易磨损:")
            for fk, fv in friction.items():
                if isinstance(fv, float):
                    print(f"    {fk}: {fv:.4f}")
                elif isinstance(fv, dict):
                    print(f"    {fk}: {json.dumps(fv, ensure_ascii=False)}")

    # 总结
    all_issues = trade_issues + acct_issues
    if all_issues:
        print(f"\n{'!' * 60}")
        print(f"发现 {len(all_issues)} 个问题:")
        for i, iss in enumerate(all_issues, 1):
            print(f"  {i}. {iss}")
    else:
        print(f"\n{'=' * 60}")
        print("验证通过: 未发现异常")

    return len(all_issues)


if __name__ == "__main__":
    sys.exit(main())
