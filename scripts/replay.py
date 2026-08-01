#!/usr/bin/env python3
"""交易回放：从 result.db 加载 debug_snapshots，输出指定 symbol/日期的决策上下文。"""

import argparse
import json
import sqlite3
import sys


def main():
    parser = argparse.ArgumentParser(description="回放回测交易决策")
    parser.add_argument("db", help="result.db 路径")
    parser.add_argument("--run-id", type=int, default=None,
                        help="run_id（缺省取最新 run）")
    parser.add_argument("--symbol", help="过滤股票代码")
    parser.add_argument("--date", help="过滤日期 YYYYMMDD")
    parser.add_argument("--list-symbols", action="store_true", help="列出有快照的日期")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    if args.run_id is None:
        try:
            row = conn.execute("SELECT MAX(run_id) FROM runs").fetchone()
        except sqlite3.OperationalError:
            row = None  # 旧库无 runs 表，回退 run 1
        if not row or row[0] is None:
            print("结果库中无 run 记录", file=sys.stderr)
            sys.exit(1)
        args.run_id = row[0]

    query = "SELECT date, snapshot_json FROM debug_snapshots WHERE run_id = ?"
    params = [args.run_id]
    if args.date:
        query += " AND date = ?"
        params.append(args.date)
    query += " ORDER BY date"

    rows = conn.execute(query, params).fetchall()
    if not rows:
        print("无匹配快照", file=sys.stderr)
        sys.exit(1)

    for date, snap_json in rows:
        snap = json.loads(snap_json)
        if args.list_symbols:
            # 收集当天涉及的 symbols
            bars = snap.get("bars_subset", {})
            holdings = snap.get("holdings_detail", {})
            symbols_of_interest = sorted(set(bars) | set(holdings))
            print(f"[{date}] {', '.join(symbols_of_interest)}")
            continue
        # Filter by symbol if requested
        bars = snap.get("bars_subset", {})
        holdings = snap.get("holdings_detail", {})
        symbols_of_interest = set(bars) | set(holdings)
        if args.symbol and args.symbol not in symbols_of_interest:
            continue

        print(f"=== {date} ===")
        print(f"账户: cash={snap['account']['cash']:.0f} "
              f"total={snap['account']['total_value']:.0f} "
              f"holdings={snap['account']['n_holdings']}")
        pending = snap.get("pending", {})
        if pending.get("buy"):
            print(f"  BUY: {pending['buy']}")
        if pending.get("sell"):
            print(f"  SELL: {pending['sell']}")
        if pending.get("buy_conditions"):
            print(f"  BUY_COND: {[c['symbol'] for c in pending['buy_conditions']]}")
        for sym, h in snap.get("holdings_detail", {}).items():
            bar = bars.get(sym, {})
            factor_keys = [k for k in bar if k not in (
                "open", "high", "low", "close", "vol", "adj_factor",
                "pre_close", "up_limit", "down_limit",
                "open_hfq", "high_hfq", "low_hfq", "close_hfq",
                "pct_chg", "amount", "trade_date",
            )]
            factor_str = ", ".join(f"{k}={bar.get(k)}" for k in factor_keys[:5])
            print(f"  {sym}: shares={h['shares']} entry={h['entry_price']} "
                  f"days={h['holding_days']} close={bar.get('close')} {factor_str}")
        print()
    conn.close()


if __name__ == "__main__":
    main()
