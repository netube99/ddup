"""TDD 审查回归 — DuckDB 迁移（行情后端与数据加载，F-MKT）。

覆盖本轮 P0/P1 修复：

- F-MKT-01 ``refresh_gold_db._asof_strict`` 严格早于对齐必须保持调用方行序，
  否则宏观列与行情行错位（``data/gold_market.duckdb`` 宏观列静默错配）。
- F-MKT-02 ``dump_brinson_data`` 行情/结果库连接必须旧格式 fail-fast
  （DuckDB 的 sqlite_scanner 会静默打开旧 SQLite 文件）。
- F-MKT-03 ``dump_fixtures`` / ``refresh_gold_db`` 行情库连接同样 fail-fast。
"""

import sqlite3
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

from scripts.refresh_gold_db import _asof_strict


def _make_sqlite(path, ddl: str) -> str:
    conn = sqlite3.connect(str(path))
    conn.execute(ddl)
    conn.commit()
    conn.close()
    return str(path)


# ═══════════════════════════════════════════
# F-MKT-01: _asof_strict 行序保持
# ═══════════════════════════════════════════


def test_asof_strict_preserves_caller_row_order():
    """调用方行序（如 DuckDB 扫描的日期降序）必须原样返回。"""
    dates = pd.Series(["20240103", "20240102", "20240101"])
    macro = pd.DataFrame(
        {"trade_date": ["20240101", "20240102", "20240103"], "v": [1.0, 2.0, 3.0]}
    )
    out = _asof_strict(dates, macro, ["v"])
    values = out["v"].tolist()
    assert values[0] == 2.0   # 20240103 严格早于 → 20240102 的值
    assert values[1] == 1.0   # 20240102 → 20240101
    assert pd.isna(values[2])  # 20240101 无更早值


def test_asof_strict_same_date_rows_share_value():
    """同一天多只 ETF（重复日期）：跨 symbol 的宏观值必须相同。"""
    dates = pd.Series(["20240103", "20240103", "20240102", "20240102"])
    macro = pd.DataFrame(
        {"trade_date": ["20240101", "20240102", "20240103"], "v": [1.0, 2.0, 3.0]}
    )
    out = _asof_strict(dates, macro, ["v"])
    assert out["v"].tolist() == [2.0, 2.0, 1.0, 1.0]


def test_asof_strict_empty_macro_keeps_order_and_nan():
    dates = pd.Series(["20240103", "20240102"])
    out = _asof_strict(dates, pd.DataFrame(), ["v"])
    assert out["v"].isna().all()
    assert len(out) == 2


# ═══════════════════════════════════════════
# F-MKT-05/06/08/09: 连接分类 / 大小写 / 重复键诊断
# ═══════════════════════════════════════════


def test_connect_market_db_corrupt_file_fails_fast(tmp_path, monkeypatch):
    """损坏的 DuckDB 文件立即报错，不按写锁冲突重试（F-MKT-05）。"""
    from btcore.generic_sql import connect_market_db

    path = tmp_path / "broken.duckdb"
    path.write_bytes(b"\x00" * 8 + b"DUCK" + b"@" + b"\x00" * 16)
    sleeps = []
    monkeypatch.setattr("btcore.generic_sql.time.sleep", sleeps.append)
    with pytest.raises(RuntimeError, match="无法只读打开行情库"):
        connect_market_db(str(path))
    assert sleeps == []  # 未重试


def test_schema_check_is_case_insensitive(tmp_path):
    """物理 MixedCase 表/列与小写表单可对接（DuckDB 标识符大小写不敏感）。"""
    from btcore.generic_sql import GenericSQLBackend

    path = tmp_path / "mixed.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        'CREATE TABLE "Quotes" ("TS_CODE" VARCHAR, "TRADE_DATE" VARCHAR,'
        ' "OPEN" DOUBLE)'
    )
    con.execute('CREATE TABLE "Cal" ("CAL_DATE" VARCHAR)')
    con.execute('CREATE TABLE "Div" ("TS_CODE" VARCHAR, "EX_DATE" VARCHAR,'
                ' "STK_DIV" DOUBLE, "CASH_DIV" DOUBLE)')
    con.execute("INSERT INTO \"Quotes\" VALUES ('000001.SZ','20240102',10.0)")
    con.close()
    form = {
        "symbol": "ts_code", "date": "trade_date",
        "open": "Quotes.open", "high": "Quotes.open", "low": "Quotes.open",
        "close": "Quotes.open", "vol": "Quotes.open",
        "adj_factor": "Quotes.open", "pre_close": "Quotes.open",
        "up_limit": "Quotes.open", "down_limit": "Quotes.open",
        "calendar_date": "Cal.cal_date",
        "dividend_ex_date": "Div.ex_date", "dividend_stk_div": "Div.stk_div",
        "dividend_cash_div": "Div.cash_div",
    }
    b = GenericSQLBackend(form, str(path))
    try:
        df = b.query_bars(None, "20240101", "20240105")
        assert len(df) == 1 and df.iloc[0]["open"] == 10.0
    finally:
        b.close()


def test_null_key_duplicates_get_actionable_message(tmp_path):
    """NULL 键在 FULL OUTER JOIN 中不匹配产生的重复行，报错应指向 NULL 键。"""
    from btcore.generic_sql import GenericSQLBackend

    path = tmp_path / "nullkey.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        'CREATE TABLE "quotes" ("ts_code" VARCHAR, "trade_date" VARCHAR,'
        ' "open" DOUBLE)'
    )
    con.execute(
        'CREATE TABLE "aux" ("ts_code" VARCHAR, "trade_date" VARCHAR,'
        ' "score" DOUBLE)'
    )
    con.execute('CREATE TABLE "cal" ("cal_date" VARCHAR)')
    con.execute(
        'CREATE TABLE "div" ("ts_code" VARCHAR, "ex_date" VARCHAR,'
        ' "stk_div" DOUBLE, "cash_div" DOUBLE)'
    )
    # 两张面板表各有一行 NULL 键（同日期）→ USING 不匹配 → 输出两行
    con.execute('INSERT INTO "quotes" VALUES (NULL, \'20240102\', 1.0)')
    con.execute('INSERT INTO "aux" VALUES (NULL, \'20240102\', 9.0)')
    con.close()
    form = {
        "symbol": "ts_code", "date": "trade_date",
        "open": "quotes.open", "high": "quotes.open", "low": "quotes.open",
        "close": "quotes.open", "vol": "quotes.open",
        "adj_factor": "quotes.open", "pre_close": "quotes.open",
        "up_limit": "quotes.open", "down_limit": "quotes.open",
        "calendar_date": "cal.cal_date",
        "dividend_ex_date": "div.ex_date", "dividend_stk_div": "div.stk_div",
        "dividend_cash_div": "div.cash_div",
        "extra_fields": {"score": "aux.score"},
    }
    b = GenericSQLBackend(form, str(path))
    try:
        with pytest.raises(ValueError, match="NULL"):
            b.query_bars(None, "20240101", "20240105", columns=["open", "score"])
    finally:
        b.close()


# ═══════════════════════════════════════════
# F-MKT-02: dump_brinson_data 旧格式拒绝
# ═══════════════════════════════════════════


def test_dump_brinson_rejects_sqlite_provider(tmp_path, monkeypatch):
    import scripts.dump_brinson_data as mod

    provider = _make_sqlite(tmp_path / "market.db", "CREATE TABLE t (a INTEGER)")
    monkeypatch.setattr(
        sys, "argv",
        ["dump_brinson_data.py", provider, "--out", str(tmp_path / "out")],
    )
    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        mod.main()


def _make_brinson_provider(path) -> str:
    conn = duckdb.connect(str(path))
    conn.execute("CREATE TABLE index_member_all (ts_code VARCHAR, l1_name VARCHAR)")
    conn.execute(
        "CREATE TABLE sw_daily (trade_date VARCHAR, name VARCHAR, pct_change DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE index_weight (index_code VARCHAR, con_code VARCHAR,"
        " trade_date VARCHAR, weight DOUBLE)"
    )
    conn.execute("INSERT INTO index_member_all VALUES ('600036.SH','银行')")
    conn.execute("INSERT INTO sw_daily VALUES ('20240603','银行',1.0)")
    conn.execute(
        "INSERT INTO index_weight VALUES ('000300.SH','600036.SH','20240603',30.0)"
    )
    conn.close()
    return str(path)


def test_dump_brinson_rejects_sqlite_result_db(tmp_path, monkeypatch):
    import scripts.dump_brinson_data as mod

    provider = _make_brinson_provider(tmp_path / "market.duckdb")
    result = _make_sqlite(tmp_path / "result.db", "CREATE TABLE trade_log (id INTEGER)")
    monkeypatch.setattr(
        sys, "argv",
        ["dump_brinson_data.py", provider, "--out", str(tmp_path / "out"),
         "--start", "20240101", "--end", "20241231", "--result-db", result],
    )
    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        mod.main()


def test_dump_brinson_valid_duckdb_provider_exports_parquets(tmp_path, monkeypatch):
    """正向回归：换成 btcore 连接助手后正常 DuckDB 路径不受影响。"""
    import scripts.dump_brinson_data as mod

    provider = _make_brinson_provider(tmp_path / "market.duckdb")
    out = tmp_path / "out"
    monkeypatch.setattr(
        sys, "argv",
        ["dump_brinson_data.py", provider, "--out", str(out),
         "--start", "20240601", "--end", "20240630"],
    )
    mod.main()
    assert (out / "industry_map.parquet").exists()
    assert (out / "sw_returns.parquet").exists()
    assert (out / "benchmark_weights.parquet").exists()


# ═══════════════════════════════════════════
# F-MKT-03: dump_fixtures / refresh_gold_db 旧格式拒绝
# ═══════════════════════════════════════════


def test_dump_fixtures_rejects_sqlite_market(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dump_fixtures.py"])
    import scripts.dump_fixtures as mod

    sqlite_market = _make_sqlite(tmp_path / "market.db", "CREATE TABLE t (a INTEGER)")
    monkeypatch.setattr(mod, "DB_PATH", sqlite_market)
    monkeypatch.setattr(mod, "FIXTURES_DIR", str(tmp_path / "fixtures"))
    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        mod.main()


def test_refresh_gold_rejects_sqlite_market(tmp_path):
    import scripts.refresh_gold_db as mod

    sqlite_market = _make_sqlite(tmp_path / "market.db", "CREATE TABLE t (a INTEGER)")
    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        mod.build(Path(sqlite_market), tmp_path / "gold.duckdb", 2024, 2024)
