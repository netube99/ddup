#!/usr/bin/env python3
"""一次性导出 Brinson 归因所需数据为本地 parquet 文件。

用法:
    python scripts/dump_brinson_data.py <行情库路径> [--out brinson_data] \
        [--index 000300.SH] [--start YYYYMMDD --end YYYYMMDD] \
        [--result-db backtest.db]

- benchmark_weights.parquet 只聚合 --index 指定指数的成分股权重
  （index_weight 表含多指数时不过滤会把 15 个指数混成一个基准）。
- 指定 --result-db 且同时给 --start/--end 时，额外导出 bars.parquet
  （该结果库交易过的股票在区间内的 close/pct_chg），供
  brinson_attribute_from_files() 离线归因直接使用。
"""

import argparse
import os
import sqlite3

import pandas as pd


def main():
    parser = argparse.ArgumentParser(description="导出 Brinson 归因数据")
    parser.add_argument("provider_db", help="tushare provider 数据库路径")
    parser.add_argument("--out", default="brinson_data", help="输出目录")
    parser.add_argument("--index", default="000300.SH",
                        help="基准指数代码（默认 000300.SH）")
    parser.add_argument("--start", default=None, help="起始日期 YYYYMMDD")
    parser.add_argument("--end", default=None, help="结束日期 YYYYMMDD")
    parser.add_argument("--result-db", default=None,
                        help="回测结果库路径；提供时（且需同时给 --start/--end）"
                             "导出 bars.parquet")
    args = parser.parse_args()

    if args.start or args.end:
        if not (args.start and args.end):
            parser.error("--start 与 --end 必须同时给出")
    date_filter = ""
    date_params: list = []
    if args.start:
        date_filter = " AND trade_date BETWEEN ? AND ?"
        date_params = [args.start, args.end]

    os.makedirs(args.out, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.provider_db}?mode=ro", uri=True)

    # industry_map: ts_code → l1_name
    df = pd.read_sql_query("SELECT ts_code, l1_name FROM index_member_all", conn)
    df.to_parquet(f"{args.out}/industry_map.parquet", index=False)
    print(f"industry_map: {len(df)} rows")

    # sw_returns: pivot to date × industry
    sql = "SELECT trade_date, name, pct_change FROM sw_daily WHERE 1=1"
    sql += date_filter
    df = pd.read_sql_query(sql, conn, params=date_params)
    df["pct_change"] = df["pct_change"].astype(float) / 100
    sw_wide = df.pivot(index="trade_date", columns="name", values="pct_change")
    sw_wide.to_parquet(f"{args.out}/sw_returns.parquet")
    print(f"sw_returns: {sw_wide.shape}")

    # benchmark_weights: 单指数成分股权重聚合到行业（避免多指数混入）
    weights = pd.read_sql_query(
        "SELECT iw.trade_date, im.l1_name, SUM(iw.weight) as weight "
        "FROM index_weight iw "
        "JOIN index_member_all im ON iw.con_code = im.ts_code "
        f"WHERE iw.index_code = ?{date_filter} "
        "GROUP BY iw.trade_date, im.l1_name",
        conn,
        params=[args.index, *date_params],
    )
    if weights.empty:
        print(f"警告: index_weight 无 {args.index} 数据")
    bw_wide = weights.pivot(index="trade_date", columns="l1_name", values="weight")
    # Normalize to sum=1 per date
    bw_wide = bw_wide.div(bw_wide.sum(axis=1), axis=0)
    bw_wide.to_parquet(f"{args.out}/benchmark_weights.parquet")
    print(f"benchmark_weights: {bw_wide.shape} (index={args.index})")

    # bars: 仅当提供结果库 + 日期区间时导出（成交过股票）
    if args.result_db:
        if not (args.start and args.end):
            conn.close()
            parser.error("--result-db 需要同时提供 --start/--end 才能导出 bars.parquet")
        bt_conn = sqlite3.connect(f"file:{args.result_db}?mode=ro", uri=True)
        try:
            traded = bt_conn.execute(
                "SELECT DISTINCT symbol FROM trade_log "
                "WHERE date >= ? AND date <= ?",
                (args.start, args.end),
            ).fetchall()
        finally:
            bt_conn.close()
        symbols = [r[0] for r in traded]
        if not symbols:
            print("警告: 结果库区间内无交易，跳过 bars.parquet")
        else:
            ph = ",".join("?" * len(symbols))
            bars = pd.read_sql_query(
                "SELECT ts_code AS symbol, trade_date, close, pct_chg "
                f"FROM stk_factor_pro WHERE ts_code IN ({ph}) "
                "AND trade_date BETWEEN ? AND ? ORDER BY trade_date, ts_code",
                conn, params=symbols + [args.start, args.end],
            )
            if not bars.empty:
                bars = bars.set_index(["trade_date", "symbol"]).sort_index()
            bars.to_parquet(f"{args.out}/bars.parquet")
            print(f"bars: {bars.shape} ({len(symbols)} symbols)")
    else:
        print("提示: 未提供 --result-db，跳过 bars.parquet "
              "（brinson_attribute_from_files 需要时请补导）")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
