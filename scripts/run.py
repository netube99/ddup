"""单策略回测 CLI。

用法:
    python scripts/run.py strategies/examples/rolling_ranker/config.yaml \
        --start 20240101 --end 20240630 [--capital N] [--out result.db] \
        [--report report.html]

行情数据库由 adapters/tushare.py 的 _DEFAULT_DB_PATH 决定。
项目假定一次只对接一个数据库，不提供运行时切换数据源的参数。
--out 回测结果库路径（默认 :memory:，不落盘）
--report HTML 报告路径（缺省生成到 <策略目录>/reports/<yaml名>_<起>_<止>.html，
         回测结果属隐私信息，该目录已加入 .gitignore；--no-report 关闭）
"""

import argparse
import sys
from pathlib import Path

from adapters.tushare import TushareBackend
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from research.report import generate_report


def _print_stats(stats: dict, indent: int = 2):
    pad = " " * indent
    for key, value in stats.items():
        if isinstance(value, dict):
            print(f"{pad}{key}:")
            _print_stats(value, indent + 2)
        elif isinstance(value, float):
            print(f"{pad}{key}: {value:.4f}")
        else:
            print(f"{pad}{key}: {value}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a YAML strategy backtest")
    parser.add_argument("yaml", help="策略 YAML 文件路径")
    parser.add_argument("--start", required=True, help="回测开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="回测结束日期 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=None, help="初始资金（覆盖 YAML config）")
    parser.add_argument("--out", default=None, help="回测结果库路径（默认内存，不落盘）")
    parser.add_argument("--report", nargs="?", const="auto", default="auto",
                        help="HTML 报告路径；缺省生成到策略目录 reports/ 下")
    parser.add_argument("--no-report", action="store_true", help="不生成报告")
    args = parser.parse_args()

    strategy = load_strategy(args.yaml)
    provider = DataProvider(TushareBackend())
    try:
        engine = Engine(strategy, provider, initial_capital=args.capital, db_path=args.out)

        result = engine.run(args.start, args.end)
        stats = result["statistics"]
        print(f"\n=== {strategy.__class__.__name__} {args.start} ~ {args.end} ===")
        _print_stats(stats)
        print(f"  trades: {len(result['trade_log'])}")
        if not args.no_report:
            if args.report == "auto":
                # 报告与策略绑定：<策略目录>/reports/<yaml名>_<起>_<止>.html
                report_dir = Path(args.yaml).resolve().parent / "reports"
                report_dir.mkdir(exist_ok=True)
                stem = Path(args.yaml).stem
                report_path = report_dir / f"{stem}_{args.start}_{args.end}.html"
            else:
                report_path = Path(args.report)
            generate_report(
                result, str(report_path),
                title=f"{strategy.__class__.__name__} {args.start} ~ {args.end}",
            )
            print(f"  report: {report_path}")
    finally:
        provider.backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

