"""一次性基准：指数成分并集 preload 裁剪 vs 全市场 preload 的性能对比。

用法:
    python scripts/bench_universe_preload.py --start 20240603 --end 20250630

对比两层:
  1. 数据加载层 — query_bars(全市场) vs query_bars(沪深300+中证500+中证1000 区间并集)
     的行数 / 耗时 / 内存
  2. 端到端 — 同一策略各跑两次 engine.run 计时（全市场 vs get_universe 裁剪）
"""

import argparse
import sys
import time
from datetime import date, timedelta

from btcore.engine import Engine
from btcore.filters import resolve_index_universe
from btcore.strategy_loader import load_strategy
from research import cli_common
from strategies.examples.rolling_ranker import RollingRanker

INDEX_CODES = ["000300.SH", "000905.SH", "000852.SH"]  # 沪深300 / 中证500 / 中证1000


def bench_load(backend, symbols: list[str] | None, start: str, end: str):
    """模拟 engine preload：含 365 天 lookback。"""
    lookback = (date.fromisoformat(start) - timedelta(days=365)).strftime("%Y%m%d")
    t0 = time.perf_counter()
    df = backend.query_bars(symbols, lookback, end)
    elapsed = time.perf_counter() - t0
    mem_mb = df.memory_usage(deep=True).sum() / 1024**2
    return len(df), df.index.get_level_values("symbol").nunique(), elapsed, mem_mb


class RollingRankerCropped(RollingRanker):
    """仅覆盖 get_universe：preload 裁剪到指数成分并集。"""

    def get_universe(self, provider, start: str, end: str) -> list[str]:
        return resolve_index_universe(provider.backend, INDEX_CODES, start, end)


def bench_engine(yaml_path: str, start: str, end: str, cropped: bool) -> float:
    strategy = load_strategy(yaml_path)
    if cropped:
        strategy.__class__ = RollingRankerCropped
    provider = cli_common.make_provider()
    try:
        engine = Engine(strategy, provider, db_path=None)
        t0 = time.perf_counter()
        engine.run(start, end)
        return time.perf_counter() - t0
    finally:
        provider.backend.close()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--yaml", default="strategies/examples/rolling_ranker/config.yaml")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--skip-load", action="store_true", help="跳过加载层基准")
    parser.add_argument("--skip-engine", action="store_true", help="跳过端到端基准")
    args = parser.parse_args()

    if not args.skip_load:
        provider = cli_common.make_provider()
        backend = provider.backend
        try:
            symbols = resolve_index_universe(backend, INDEX_CODES,
                                             args.start, args.end)
            print(f"指数成分并集: {len(symbols)} 只 (指数: {', '.join(INDEX_CODES)})",
                  flush=True)

            # 预热 page cache，避免第一次查询白挨冷盘
            bench_load(backend, None, args.start, args.end)

            for label, syms in [("全市场", None), ("成分并集", symbols)]:
                rows, nsym, t, mem = bench_load(backend, syms, args.start, args.end)
                print(f"  preload[{label}]: {rows:>9,} 行 / {nsym:>5} 只"
                      f" / {t:6.2f}s / {mem:7.1f}MB", flush=True)
        finally:
            backend.close()

    if not args.skip_engine:
        # 端到端：交替各跑两次，平滑缓存噪声
        for rnd in (1, 2):
            for label, cropped in [("全市场", False), ("成分并集", True)]:
                t = bench_engine(args.yaml, args.start, args.end, cropped)
                print(f"  engine[{label}] 第{rnd}次: {t:6.2f}s", flush=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
