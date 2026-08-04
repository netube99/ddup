"""
Cross-Validation — 回测结果交叉验证（薄壳 CLI）。

校验逻辑在 research/cross_validate.py（可 import 库，测试直接 import）。

用法：
  .venv/bin/python scripts/cross_validate.py result.db --strategy smart_money
"""

import argparse
import json
import sys

from research.cross_validate import load_backtest, validate_daily, validate_trades


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
