"""Dump market database to parquet fixtures for testing.

Usage: python scripts/dump_fixtures.py

Outputs to tests/fixtures/:
    bars.parquet       - stk_factor_pro for 000300.SH + dividend 送股 stocks (projected fields)
    dividends.parquet  - dividend table for relevant symbols
    st.parquet         - stock_st for relevant symbols (wider window)
    limits.parquet     - stk_limit for relevant symbols + 创业板切换边界
    components.parquet - index_weight for 000300.SH
    benchmark_bars.parquet - 000300.SH 指数点位（hfq_close = close，无复权概念）
    trade_cal.parquet  - SSE calendar
    moneyflow.parquet / cyq_perf.parquet / margin_detail.parquet - aux 日线表
"""

import os
import sqlite3

import pandas as pd

from adapters.tushare import _DEFAULT_DB_PATH

DB_PATH = _DEFAULT_DB_PATH
if not DB_PATH:
    raise SystemExit("错误: 请在 adapters/tushare.py 中设置 _DEFAULT_DB_PATH")
FIXTURES_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "tests", "fixtures")

TARGET_START = "20240601"
TARGET_END = "20240701"
BENCHMARK_CODE = "000300.SH"
INDEX_CODE = "000300.SH"

# Extra window for 创业板 20200824 switching boundary
EXTRA_LIMITS_START = "20200820"
EXTRA_LIMITS_END = "20200825"

# Columns to include in bars fixture - required + common factor fields
BAR_COLUMNS = [
    "ts_code", "trade_date",
    "open", "high", "low", "close",
    "open_hfq", "high_hfq", "low_hfq", "close_hfq",
    "pre_close", "pct_chg",
    "vol", "amount", "adj_factor",
    "turnover_rate", "volume_ratio",
    "pe", "pe_ttm", "pb", "ps", "ps_ttm",
    "dv_ratio", "dv_ttm",
    "total_mv", "circ_mv",
    "atr_hfq", "rsi_hfq_12", "bias2_hfq", "roc_hfq", "ma_hfq_20",
    "ema_hfq_5", "ema_hfq_10", "ema_hfq_20", "ema_hfq_60",
]


def main():
    os.makedirs(FIXTURES_DIR, exist_ok=True)

    uri = f"file:{DB_PATH}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)

    # ---- 1. Determine symbol list ----
    main_symbols = _get_000300_components(conn, TARGET_START, TARGET_END)
    print(f"000300.SH components in range: {len(main_symbols)}")

    div_symbols = _get_dividend_symbols(conn, TARGET_START, TARGET_END)
    print(f"送股 symbols in range: {len(div_symbols)}")

    all_bar_symbols = list(set(main_symbols) | set(div_symbols))

    # ---- 2. Bars ----
    print("\n[Dump] bars.parquet ...")
    bars_df = _dump_bars(conn, all_bar_symbols, TARGET_START, TARGET_END)
    bars_path = os.path.join(FIXTURES_DIR, "bars.parquet")
    bars_df.to_parquet(bars_path)
    size_mb = os.path.getsize(bars_path) / (1024 * 1024)
    print(f"  rows={len(bars_df)} cols={len(bars_df.columns)} size={size_mb:.2f} MB")

    # ---- 3. Dividends ----
    print("\n[Dump] dividends.parquet ...")
    div_df = _dump_dividends(conn, all_bar_symbols, TARGET_START, TARGET_END)
    div_path = os.path.join(FIXTURES_DIR, "dividends.parquet")
    div_df.to_parquet(div_path)
    print(f"  rows={len(div_df)} size={os.path.getsize(div_path)/1024:.1f} KB")

    # ---- 4. ST ----
    print("\n[Dump] st.parquet ...")
    st_df = _dump_st(conn, main_symbols, TARGET_START, TARGET_END)
    st_path = os.path.join(FIXTURES_DIR, "st.parquet")
    st_df.to_parquet(st_path)
    print(f"  rows={len(st_df)} size={os.path.getsize(st_path)/1024:.1f} KB")

    # ---- 5. Limits ----
    print("\n[Dump] limits.parquet ...")
    limits_df = _dump_limits(conn, all_bar_symbols,
                             TARGET_START, TARGET_END,
                             extra_start=EXTRA_LIMITS_START, extra_end=EXTRA_LIMITS_END)
    limits_path = os.path.join(FIXTURES_DIR, "limits.parquet")
    limits_df.to_parquet(limits_path)
    print(f"  rows={len(limits_df)} size={os.path.getsize(limits_path)/1024:.1f} KB")

    # ---- 6. Components ----
    print("\n[Dump] components.parquet ...")
    comp_df = _dump_components(conn, INDEX_CODE, TARGET_START, TARGET_END)
    comp_path = os.path.join(FIXTURES_DIR, "components.parquet")
    comp_df.to_parquet(comp_path)
    print(f"  rows={len(comp_df)} size={os.path.getsize(comp_path)/1024:.1f} KB")

    # ---- 7. Benchmark ----
    print("\n[Dump] benchmark_bars.parquet ...")
    bench_df = _dump_benchmark(conn, BENCHMARK_CODE, TARGET_START, TARGET_END)
    bench_path = os.path.join(FIXTURES_DIR, "benchmark_bars.parquet")
    bench_df.to_parquet(bench_path)
    print(f"  rows={len(bench_df)} size={os.path.getsize(bench_path)/1024:.1f} KB")

    # ---- 8. Trade Calendar ----
    print("\n[Dump] trade_cal.parquet ...")
    cal_df = _dump_trade_cal(conn, TARGET_START, TARGET_END)
    cal_path = os.path.join(FIXTURES_DIR, "trade_cal.parquet")
    cal_df.to_parquet(cal_path)
    print(f"  rows={len(cal_df)} size={os.path.getsize(cal_path)/1024:.1f} KB")

    # ---- 9. Aux tables (moneyflow, cyq_perf, margin_detail) ----
    for tbl in ["moneyflow", "cyq_perf", "margin_detail"]:
        print(f"\n[Dump] {tbl}.parquet ...")
        df = _dump_aux_table(conn, tbl, all_bar_symbols, TARGET_START, TARGET_END)
        path = os.path.join(FIXTURES_DIR, f"{tbl}.parquet")
        df.to_parquet(path)
        print(f"  rows={len(df)} cols={len(df.columns)} "
              f"size={os.path.getsize(path)/1024:.1f} KB")

    conn.close()

    # ---- Summary ----
    print("\n=== Fixtures Summary ===")
    total_size = sum(os.path.getsize(os.path.join(FIXTURES_DIR, f))
                     for f in os.listdir(FIXTURES_DIR) if f.endswith(".parquet"))
    print(f"Total size: {total_size / (1024*1024):.2f} MB")
    print("Done.")


def _get_000300_components(conn, start, end):
    rows = conn.execute(
        "SELECT DISTINCT con_code FROM index_weight "
        "WHERE index_code=? AND trade_date BETWEEN ? AND ?",
        (INDEX_CODE, start, end),
    ).fetchall()
    return [r[0] for r in rows]


def _get_dividend_symbols(conn, start, end):
    """Find symbols with 送股 dividends to test INV6 corporate actions."""
    rows = conn.execute(
        "SELECT DISTINCT ts_code FROM dividend "
        "WHERE div_proc='实施' AND ex_date IS NOT NULL AND stk_div > 0 "
        "AND ex_date BETWEEN ? AND ?",
        (start, end),
    ).fetchall()
    return [r[0] for r in rows]


def _dump_bars(conn, symbols, start, end):
    """Dump projected stk_factor_pro columns for given symbols and date range.

    eps 从 bak_basic 联表（exclude_loss 过滤规则依赖；tushare 亏损股
    pe_ttm 为 NULL 或正数，eps<0 才是可靠亏损信号）。
    """
    if not symbols:
        return pd.DataFrame()

    fields = ", ".join(BAR_COLUMNS)
    placeholders = ",".join("?" * len(symbols))
    sql = (
        f"SELECT {fields}, b.eps AS eps FROM stk_factor_pro s "
        "LEFT JOIN bak_basic b ON b.ts_code = s.ts_code "
        "AND b.trade_date = s.trade_date "
        f"WHERE s.ts_code IN ({placeholders}) "
        "AND s.trade_date BETWEEN ? AND ?"
    )
    df = pd.read_sql_query(sql, conn, params=symbols + [start, end])
    df.rename(columns={"ts_code": "symbol"}, inplace=True)
    return df


def _dump_dividends(conn, symbols, start, end):
    """Dump dividend rows for relevant symbols."""
    if not symbols:
        return pd.DataFrame()

    placeholders = ",".join("?" * len(symbols))
    sql = (
        "SELECT ts_code, stk_div, cash_div, ex_date, ann_date, div_proc "
        "FROM dividend "
        f"WHERE ts_code IN ({placeholders}) "
        "AND div_proc='实施' AND ex_date IS NOT NULL "
        "AND ex_date BETWEEN ? AND ?"
    )
    return pd.read_sql_query(sql, conn, params=symbols + [start, end])


def _dump_st(conn, symbols, start, end):
    """Dump stock_st for all 000300.SH historical symbols, wider window.

    Note: stock_st.type uses 'ST', not 'S'.
    """
    # Get ALL 000300.SH symbols ever (wider set than the June 2024 slice)
    all_syms = conn.execute(
        "SELECT DISTINCT con_code FROM index_weight "
        "WHERE index_code=? AND trade_date BETWEEN ? AND ?",
        (INDEX_CODE, _add_years(start, -2), end),
    ).fetchall()
    all_st_symbols = list(set(r[0] for r in all_syms))

    if not all_st_symbols:
        return pd.DataFrame()

    preload_start = _add_years(start, -2)
    placeholders = ",".join("?" * len(all_st_symbols))
    sql = (
        "SELECT ts_code, trade_date, type "
        "FROM stock_st "
        f"WHERE ts_code IN ({placeholders}) "
        "AND trade_date BETWEEN ? AND ? AND type='ST'"
    )
    return pd.read_sql_query(sql, conn, params=all_st_symbols + [preload_start, end])


def _add_years(date_str, offset):
    """Add years to date string."""
    y, m, d = int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8])
    return f"{y + offset}{m:02d}{d:02d}"


def _dump_limits(conn, symbols, start, end, extra_start, extra_end):
    """Dump stk_limit for main window + extra 创业板切换 window."""
    if not symbols:
        return pd.DataFrame()

    placeholders = ",".join("?" * len(symbols))

    # Main window
    sql_main = (
        "SELECT trade_date, ts_code, up_limit, down_limit, pre_close "
        "FROM stk_limit "
        f"WHERE ts_code IN ({placeholders}) "
        "AND trade_date BETWEEN ? AND ?"
    )
    df_main = pd.read_sql_query(sql_main, conn, params=symbols + [start, end])

    # Extra 创业板 switching window (20200820-20200825) - use broader symbol set
    extra_300_symbols_df = pd.read_sql_query(
        "SELECT DISTINCT ts_code FROM stk_limit "
        "WHERE (ts_code LIKE '300%' OR ts_code LIKE '301%') "
        "AND trade_date BETWEEN ? AND ?",
        conn, params=[extra_start, extra_end],
    )
    extra_syms = extra_300_symbols_df["ts_code"].tolist()
    if extra_syms:
        ep = ",".join("?" * len(extra_syms))
        df_extra = pd.read_sql_query(
            "SELECT trade_date, ts_code, up_limit, down_limit, pre_close "
            "FROM stk_limit "
            f"WHERE ts_code IN ({ep}) "
            "AND trade_date BETWEEN ? AND ?",
            conn, params=extra_syms + [extra_start, extra_end],
        )
    else:
        df_extra = pd.DataFrame()

    return pd.concat([df_main, df_extra], ignore_index=True).drop_duplicates()


def _dump_components(conn, index_code, start, end):
    """Dump index_weight for the target index."""
    df = pd.read_sql_query(
        "SELECT index_code, con_code, trade_date, weight "
        "FROM index_weight "
        "WHERE index_code=? AND trade_date BETWEEN ? AND ?",
        conn, params=[index_code, start, end],
    )
    return df


def _dump_benchmark(conn, index_code, start, end):
    """Dump 000300.SH 指数点位作为基准（hfq_close = close，指数无复权概念）。"""
    df = pd.read_sql_query(
        "SELECT trade_date, close FROM idx_factor_pro "
        "WHERE ts_code=? AND trade_date BETWEEN ? AND ? ORDER BY trade_date",
        conn, params=[index_code, start, end],
    )
    df["hfq_close"] = df["close"]
    return df[["trade_date", "hfq_close"]]


def _dump_trade_cal(conn, start, end):
    """Dump SSE trade calendar."""
    df = pd.read_sql_query(
        "SELECT cal_date, is_open, exchange FROM trade_cal "
        "WHERE exchange='SSE' AND cal_date BETWEEN ? AND ? "
        "ORDER BY cal_date",
        conn, params=[start, end],
    )
    return df


def _dump_aux_table(conn, table: str, symbols: list[str], start: str, end: str):
    """Dump all columns from an auxiliary factor table."""
    if not symbols:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(symbols))
    sql = (
        f"SELECT * FROM {table} "
        f"WHERE ts_code IN ({placeholders}) "
        "AND trade_date BETWEEN ? AND ?"
    )
    return pd.read_sql_query(sql, conn, params=symbols + [start, end])


if __name__ == "__main__":
    main()
