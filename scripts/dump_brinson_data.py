#!/usr/bin/env python3
"""一次性导出 Brinson 归因所需数据为本地 parquet 文件。"""
import argparse
import os
import sqlite3

import pandas as pd


def main():
    parser = argparse.ArgumentParser(description="导出 Brinson 归因数据")
    parser.add_argument("provider_db", help="tushare provider 数据库路径")
    parser.add_argument("--out", default="brinson_data", help="输出目录")
    args = parser.parse_args()

    os.makedirs(args.out, exist_ok=True)

    conn = sqlite3.connect(f"file:{args.provider_db}?mode=ro", uri=True)

    # industry_map: ts_code → l1_name
    df = pd.read_sql_query("SELECT ts_code, l1_name FROM index_member_all", conn)
    df.to_parquet(f"{args.out}/industry_map.parquet", index=False)
    print(f"industry_map: {len(df)} rows")

    # sw_returns: pivot to date × industry
    df = pd.read_sql_query("SELECT trade_date, name, pct_change FROM sw_daily", conn)
    df["pct_change"] = df["pct_change"].astype(float) / 100
    sw_wide = df.pivot(index="trade_date", columns="name", values="pct_change")
    sw_wide.to_parquet(f"{args.out}/sw_returns.parquet")
    print(f"sw_returns: {sw_wide.shape}")

    # benchmark_weights: aggregate index_weight to industry level
    # Join with index_member_all to get industry, then aggregate
    weights = pd.read_sql_query(
        "SELECT iw.trade_date, im.l1_name, SUM(iw.weight) as weight "
        "FROM index_weight iw "
        "JOIN index_member_all im ON iw.con_code = im.ts_code "
        "GROUP BY iw.trade_date, im.l1_name",
        conn,
    )
    bw_wide = weights.pivot(index="trade_date", columns="l1_name", values="weight")
    # Normalize to sum=1 per date
    bw_wide = bw_wide.div(bw_wide.sum(axis=1), axis=0)
    bw_wide.to_parquet(f"{args.out}/benchmark_weights.parquet")
    print(f"benchmark_weights: {bw_wide.shape}")

    conn.close()
    print("Done.")


if __name__ == "__main__":
    main()
