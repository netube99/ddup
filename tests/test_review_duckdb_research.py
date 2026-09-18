"""TDD 审查回归 — DuckDB 迁移（研究层：报告/回放/归因/交叉验证）。

每条测试对应一份审查发现（F-RPT-NN），先红后绿：

- F-RPT-01/02 归因路径禁用裸 duckdb.connect：sqlite_scanner 会静默打开旧
  SQLite 文件（docs/backend_guide.md §10.1「旧格式拒绝」），必须走
  btcore 连接助手（assert_duckdb_file + 锁重试）。
- F-RPT-03/04 读路径禁用读写连接：init_backtest_db 会执行 DELETE FROM
  holdings 与 DDL；同进程已有只读连接时 raise ConnectionException，
  跨进程有其他读者时 raise IOException（Conflicting lock）。纯读入口只
  应 connect_result_db(read_only=True)。
"""

import json
import sqlite3

import duckdb
import pandas as pd
import pytest

from btcore import database
from research import attribution, report
from research.cross_validate import load_backtest
from research.replay import run_replay


def _make_sqlite(path, ddl: str = "CREATE TABLE runs (run_id INTEGER)") -> str:
    conn = sqlite3.connect(str(path))
    conn.execute(ddl)
    conn.commit()
    conn.close()
    return str(path)


def _make_duckdb(path) -> str:
    duckdb.connect(str(path)).close()
    return str(path)


def _seed_result_db(path, *, with_holdings: bool = True) -> str:
    conn = database.init_backtest_db(str(path))
    database.write_run(
        conn, created_at="2026", strategy="demo", start_date="20240603",
        end_date="20240607", initial_capital=100000,
        config_json=json.dumps({"initial_capital": 100000}), status="completed",
    )
    conn.execute(
        "INSERT INTO account_daily (run_id, date, cash, total_value, daily_pnl,"
        " initial_capital, n_holdings) VALUES (?,?,?,?,?,?,?)",
        [1, "20240603", 90000, 100000, 0, 100000, 1],
    )
    conn.execute(
        "INSERT INTO trade_log (run_id,date,symbol,side,trigger,price,shares,"
        "turnover,commission,stamp_tax,transfer_fee,slippage_amount,"
        "net_amount,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        [1, "20240603", "000001.SZ", "BUY", "MANUAL", 10.0, 10000, 100000.0,
         5.0, 0.0, 0.0, 0.0, -100005.0, ""],
    )
    if with_holdings:
        conn.execute(
            "INSERT INTO holdings (symbol, entry_date, entry_price, shares, cost)"
            " VALUES ('000001.SZ', '20240603', 10.0, 10000, 100000.0)"
        )
    conn.close()
    return str(path)


# ═══════════════════════════════════════════
# F-RPT-01/02 归因连接：旧格式 fail-fast
# ═══════════════════════════════════════════


def test_attribution_provider_rejects_sqlite(tmp_path):
    """F-RPT-01: 行情库连接必须经 connect_market_db 拒绝旧 SQLite 文件。"""
    sqlite_market = _make_sqlite(tmp_path / "market.sqlite")
    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        attribution._open_provider_db(sqlite_market)


def test_attribution_provider_missing_file_raises_filenotfound(tmp_path):
    """F-RPT-01: 缺文件应是可操作的 FileNotFoundError，而非裸 DuckDB IOException。"""
    with pytest.raises(FileNotFoundError):
        attribution._open_provider_db(str(tmp_path / "no_such.duckdb"))


def test_brinson_attribute_rejects_sqlite_result_db(tmp_path):
    """F-RPT-02: 结果库裸 connect 会静默打开 SQLite；须直接 ValueError。"""
    provider = _make_duckdb(tmp_path / "market.duckdb")
    result = _make_sqlite(
        tmp_path / "result.sqlite", "CREATE TABLE trade_log (id INTEGER)"
    )
    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        attribution.brinson_attribute(result, provider, "20240101", "20240131")


def test_brinson_attribute_rejects_sqlite_provider(tmp_path):
    """F-RPT-01: 行情库旧格式在连接期报错（不进入查询阶段）。"""
    result = _seed_result_db(tmp_path / "result.duckdb")
    provider = _make_sqlite(
        tmp_path / "market.sqlite",
        "CREATE TABLE index_member_all (ts_code TEXT, l1_name TEXT, l1_code TEXT)",
    )
    with pytest.raises(ValueError, match="行情库"):
        attribution.brinson_attribute(result, provider, "20240101", "20240131")


def test_brinson_attribute_from_files_rejects_sqlite_result_db(tmp_path):
    """F-RPT-02: 离线归因入口同样不得静默打开 SQLite 结果库。"""
    out = tmp_path / "data"
    out.mkdir()
    pd.DataFrame({"ts_code": ["600036.SH"], "l1_name": ["银行"]}).to_parquet(
        out / "industry_map.parquet", index=False
    )
    pd.DataFrame({"银行": [0.01]}, index=["20240603"]).to_parquet(
        out / "sw_returns.parquet"
    )
    pd.DataFrame({"银行": [0.3]}, index=["20240603"]).to_parquet(
        out / "benchmark_weights.parquet"
    )
    bars = pd.DataFrame(
        {"close": [35.0], "pct_chg": [2.0]},
        index=pd.MultiIndex.from_tuples(
            [("20240603", "600036.SH")], names=["trade_date", "symbol"]
        ),
    )
    bars.to_parquet(out / "bars.parquet")
    result = _make_sqlite(
        tmp_path / "result.sqlite", "CREATE TABLE trade_log (id INTEGER)"
    )

    with pytest.raises(ValueError, match="不是 DuckDB 格式"):
        attribution.brinson_attribute_from_files(
            result_db=result,
            industry_map=str(out / "industry_map.parquet"),
            sw_returns=str(out / "sw_returns.parquet"),
            benchmark_weights=str(out / "benchmark_weights.parquet"),
            bars=str(out / "bars.parquet"),
            run_id=1,
        )


def _make_synthetic_market(path) -> str:
    """最小行情库（index_member_all/sw_daily/index_weight/stk_factor_pro）。"""
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE index_member_all (ts_code VARCHAR, l1_name VARCHAR,"
        " l1_code VARCHAR)"
    )
    conn.execute(
        "INSERT INTO index_member_all VALUES"
        " ('600036.SH','银行','801780.SI'), ('600519.SH','食品饮料','801010.SI'),"
        " ('000001.SZ','银行','801780.SI')"
    )
    conn.execute(
        "CREATE TABLE sw_daily (trade_date VARCHAR, ts_code VARCHAR,"
        " name VARCHAR, pct_change DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE index_weight (index_code VARCHAR, con_code VARCHAR,"
        " trade_date VARCHAR, weight DOUBLE)"
    )
    conn.execute(
        "CREATE TABLE stk_factor_pro (ts_code VARCHAR, trade_date VARCHAR,"
        " close DOUBLE, pct_chg DOUBLE)"
    )
    for i, d in enumerate(
        ("20240603", "20240604", "20240605", "20240606", "20240607")
    ):
        conn.execute(
            "INSERT INTO sw_daily VALUES (?,?,?,?)", [d, "801780.SI", "银行", 1.0]
        )
        conn.execute(
            "INSERT INTO sw_daily VALUES (?,?,?,?)",
            [d, "801010.SI", "食品饮料", 2.0],
        )
        conn.execute(
            "INSERT INTO stk_factor_pro VALUES ('600036.SH',?,?,?)",
            [d, 35.0 * (1 + 0.02 * i), 2.0],
        )
        conn.execute(
            "INSERT INTO stk_factor_pro VALUES ('600519.SH',?,?,?)",
            [d, 1600.0 * (1 + 0.02 * i), 5.0],
        )
        conn.execute(
            "INSERT INTO stk_factor_pro VALUES ('000001.SZ',?,?,?)",
            [d, 10.0 * (1 + 0.02 * i), 2.0],
        )
    for d in ("20240603", "20240607"):
        conn.execute(
            "INSERT INTO index_weight VALUES ('000300.SH','600036.SH',?,30.0)", [d]
        )
        conn.execute(
            "INSERT INTO index_weight VALUES ('000300.SH','600519.SH',?,20.0)", [d]
        )
    conn.close()
    return str(path)


def test_brinson_attribute_full_path_on_valid_duckdb(tmp_path):
    """F-RPT-01/02 修复的正向回归：合法 DuckDB 行情+结果库全链路可用。"""
    market = _make_synthetic_market(tmp_path / "market.duckdb")
    result = _seed_result_db(tmp_path / "result.duckdb")

    res = attribution.brinson_attribute(result, market, "20240603", "20240607")

    assert "error" not in res, res.get("error")
    assert res["summary"]["total_benchmark_return"] > 0
    assert "银行" in res["industry_detail"]


# ═══════════════════════════════════════════
# F-RPT-03/04 读路径：不得开读写连接/不得静默写删
# ═══════════════════════════════════════════


def test_load_backtest_coexists_with_open_readonly_connection(tmp_path):
    """F-RPT-04: 调用方已持只读连接时，load_backtest 不得因读写连接冲突崩溃。"""
    db = _seed_result_db(tmp_path / "cv.duckdb")
    ro = database.connect_result_db(db, read_only=True)
    try:
        trades, daily, stats, config, run = load_backtest(db, 1)
    finally:
        ro.close()
    assert run["run_id"] == 1
    assert config["initial_capital"] == 100000
    assert len(trades) == 1


def test_load_backtest_missing_db_does_not_create_file(tmp_path):
    """F-RPT-04: 读路径对不存在的库应报错而非创建空库。"""
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(duckdb.Error):
        load_backtest(str(missing))
    assert not missing.exists()


def test_load_runs_coexists_with_open_readonly_and_keeps_holdings(tmp_path):
    """F-RPT-03: load_runs 是纯读入口——不得与已有只读连接冲突，不得清空 holdings。"""
    db = _seed_result_db(tmp_path / "report.duckdb")
    ro = database.connect_result_db(db, read_only=True)
    try:
        runs = report.load_runs(db)
    finally:
        ro.close()
    assert len(runs) == 1

    chk = database.connect_result_db(db, read_only=True)
    try:
        n_holdings = chk.execute("SELECT COUNT(*) FROM holdings").fetchone()[0]
    finally:
        chk.close()
    assert n_holdings == 1


def test_load_runs_missing_db_does_not_create_file(tmp_path):
    """F-RPT-03: 读路径对不存在的库应报错而非创建空库。"""
    missing = tmp_path / "missing.duckdb"
    with pytest.raises(duckdb.Error):
        report.load_runs(str(missing))
    assert not missing.exists()


# ═══════════════════════════════════════════
# CLI 行为回归（迁移后保持）
# ═══════════════════════════════════════════


def test_generate_report_from_empty_schema_db_raises(tmp_path):
    """空 run 的结果库必须报 ValueError（report CLI 报错路径）。"""
    db = tmp_path / "empty.duckdb"
    database.init_backtest_db(str(db)).close()
    with pytest.raises(ValueError, match="没有可用 run"):
        report.generate_report_from_db(str(db), str(tmp_path / "r.html"))


def test_compare_report_needs_two_runs(tmp_path):
    """对比报告少于 2 个 run 必须报 ValueError（compare CLI 报错路径）。"""
    db = _seed_result_db(tmp_path / "one.duckdb")
    with pytest.raises(ValueError, match="至少需要 2 个 run"):
        report.generate_compare_report(db, str(tmp_path / "cmp.html"))


def test_replay_missing_file_reports_and_exits_one(tmp_path, capsys):
    """回放入口对不存在的库：stderr 可操作报错 + 退出码 1（不再裸 traceback）。"""
    rc = run_replay(str(tmp_path / "missing.duckdb"))
    assert rc == 1
    err = capsys.readouterr().err
    assert "无法打开结果库" in err and "missing.duckdb" in err


# ═══════════════════════════════════════════
# F-RPT-06/07/08: 归因取数单扫描 / 注册关系 / 连接关闭
# ═══════════════════════════════════════════


def test_industry_map_single_scan_returns_codes(tmp_path):
    """F-RPT-07: _load_industry_map 单次扫描同时产出映射与 L1 代码。"""
    path = tmp_path / "market.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE index_member_all (ts_code VARCHAR, l1_name VARCHAR,"
        " l1_code VARCHAR)"
    )
    con.executemany(
        "INSERT INTO index_member_all VALUES (?,?,?)",
        [("600036.SH", "银行", "801780.SI"), ("600519.SH", "食品饮料", "801120.SI"),
         ("000001.SZ", "银行", "801780.SI")],
    )
    con.close()
    conn = database.connect_result_db(str(path), read_only=True)
    try:
        industry_map, l1_codes = attribution._load_industry_map(conn)
    finally:
        conn.close()
    assert industry_map == {"600036.SH": "银行", "600519.SH": "食品饮料",
                            "000001.SZ": "银行"}
    assert l1_codes == ["801120.SI", "801780.SI"]


def test_load_bars_for_symbols_registered_relation(tmp_path):
    """F-RPT-06: 注册关系半连接取数，结果与逐符号 IN 等价。"""
    path = tmp_path / "market.duckdb"
    con = duckdb.connect(str(path))
    con.execute(
        "CREATE TABLE stk_factor_pro (ts_code VARCHAR, trade_date VARCHAR,"
        " close DOUBLE, pct_chg DOUBLE)"
    )
    con.executemany(
        "INSERT INTO stk_factor_pro VALUES (?,?,?,?)",
        [("000001.SZ", "20240603", 10.0, 1.0),
         ("000001.SZ", "20240604", 10.5, 5.0),
         ("600000.SH", "20240603", 20.0, -1.0)],
    )
    con.close()
    conn = database.connect_result_db(str(path), read_only=True)
    try:
        df = attribution._load_bars_for_symbols(
            conn, ["000001.SZ"], "20240603", "20240604"
        )
    finally:
        conn.close()
    assert list(df.index) == [("20240603", "000001.SZ"), ("20240604", "000001.SZ")]
    assert df["close"].tolist() == [10.0, 10.5]
