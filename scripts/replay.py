#!/usr/bin/env python3
"""交易回放 CLI（薄壳）— 逻辑在 research/replay.py。"""

import argparse
import sys

from research.replay import run_replay


def main() -> int:
    parser = argparse.ArgumentParser(description="回放回测交易决策")
    parser.add_argument("db", help="result.db 路径")
    parser.add_argument("--run-id", type=int, default=None,
                        help="run_id（缺省取最新 run）")
    parser.add_argument("--symbol", help="过滤股票代码")
    parser.add_argument("--date", help="过滤日期 YYYYMMDD")
    parser.add_argument("--list-symbols", action="store_true", help="列出有快照的日期")
    args = parser.parse_args()

    return run_replay(args.db, args.run_id, symbol=args.symbol,
                      date=args.date, list_symbols=args.list_symbols)


if __name__ == "__main__":
    sys.exit(main())
