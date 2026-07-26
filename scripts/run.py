"""单策略回测 CLI。

用法:
    python scripts/run.py strategies/examples/topk_momentum.yaml \
        --start 20240101 --end 20240630 [--capital N] [--out result.db]

行情数据库由 adapters/tushare.py 的 _DEFAULT_DB_PATH 决定。
项目假定一次只对接一个数据库，不提供运行时切换数据源的参数。
--out 回测结果库路径（默认 :memory:，不落盘）
"""

import argparse
import sys

from adapters.tushare import TushareBackend
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a YAML strategy backtest")
    parser.add_argument("yaml", help="策略 YAML 文件路径")
    parser.add_argument("--start", required=True, help="回测开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="回测结束日期 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=None, help="初始资金（覆盖 YAML config）")
    parser.add_argument("--out", default=None, help="回测结果库路径（默认内存，不落盘）")
    args = parser.parse_args()

    strategy = load_strategy(args.yaml)
    provider = DataProvider(TushareBackend())
    try:
        engine = Engine(strategy, provider, initial_capital=args.capital, db_path=args.out)

        result = engine.run(args.start, args.end)
        stats = result["statistics"]
        print(f"\n=== {strategy.__class__.__name__} {args.start} ~ {args.end} ===")
        for key, value in stats.items():
            if isinstance(value, float):
                print(f"  {key}: {value:.4f}")
            else:
                print(f"  {key}: {value}")
        print(f"  trades: {len(result['trade_log'])}")
    finally:
        provider.backend.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
