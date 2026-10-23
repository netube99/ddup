"""连接注入 + 异构上游（SQLite ATTACH / CSV VIEW）接入测试。

覆盖 GenericSQLBackend 的 conn= 注入路径：
- db_path 与 conn 互斥
- SQLite ATTACH 后三段式 "库名.表名.字段名" 表单（schema/键校验、
  query_bars 拼表、日历、分红）
- CSV read_csv VIEW + 清洗层（ISO 日期 cast、数字代码补零、缺失列 NULL）
- 脏键（DATE/BIGINT）被 _check_key_types fail-fast 拒绝
- 注入连接归调用方持有：close() 后连接仍可用
"""

import duckdb
import pandas as pd
import pytest

from btcore.generic_sql import GenericSQLBackend

# ── SQLite 上游：标准库建库，DuckDB ATTACH 挂载 ──

SQLITE_FORM = {
    "symbol": "ts_code",
    "date": "trade_date",
    "open": "src.quotes.open",
    "high": "src.quotes.high",
    "low": "src.quotes.low",
    "close": "src.quotes.close",
    "vol": "src.quotes.vol",
    "adj_factor": "src.adj.adj_factor",
    "pre_close": "src.quotes.pre_close",
    "up_limit": "src.quotes.up_limit",
    "down_limit": "src.quotes.down_limit",
    "calendar_date": "src.cal.cal_date",
    "dividend_ex_date": "src.div.ex_date",
    "dividend_stk_div": "src.div.stk_div",
    "dividend_cash_div": "src.div.cash_div",
}


def _build_sqlite(path):
    import sqlite3
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE quotes (ts_code TEXT, trade_date TEXT, open REAL, high REAL,
                         low REAL, close REAL, vol REAL, pre_close REAL,
                         up_limit REAL, down_limit REAL);
    CREATE TABLE adj (ts_code TEXT, trade_date TEXT, adj_factor REAL);
    CREATE TABLE cal (cal_date TEXT);
    CREATE TABLE div (ts_code TEXT, ex_date TEXT, stk_div REAL, cash_div REAL);
    """)
    conn.execute("INSERT INTO quotes VALUES ('000001.SZ','20240102',10.0,10.8,"
                 "9.9,10.5,1000,10.0,11.0,9.0)")
    conn.execute("INSERT INTO quotes VALUES ('600000.SH','20240102',8.0,8.5,"
                 "7.8,8.2,2000,8.0,8.8,7.2)")
    conn.execute("INSERT INTO adj VALUES ('000001.SZ','20240102',1.5)")
    conn.execute("INSERT INTO adj VALUES ('600000.SH','20240102',1.0)")
    conn.executemany("INSERT INTO cal VALUES (?)", [("20240101",), ("20240102",)])
    conn.execute("INSERT INTO div VALUES ('000001.SZ','20240103',0.0,0.5)")
    conn.commit()
    conn.close()


def _attach_sqlite_or_skip(conn, db_path):
    try:
        conn.execute(f"ATTACH '{db_path}' AS src (TYPE SQLITE)")
    except duckdb.Error as exc:
        pytest.skip(f"sqlite_scanner 不可用: {exc}")


# ── CSV 上游：脏数据 + 清洗 VIEW ──

# 刻意脏：代码丢前导零（1 → 000001.SZ）、日期 ISO 带横线、无涨跌停列
DIRTY_CSV = (
    "code,dt,open,high,low,close,vol,adj_factor,pre_close\n"
    "1,2024-01-02,10.0,10.8,9.9,10.5,1000,1.5,10.0\n"
    "600000,2024-01-02,8.0,8.5,7.8,8.2,2000,1.0,8.0\n"
)
CAL_CSV = "cal_date\n20240101\n20240102\n"
DIV_CSV = "code,ex_date,stk_div,cash_div\n1,20240103,0,0.5\n"


def _csv_form():
    return {
        "symbol": "ts_code",
        "date": "trade_date",
        "open": "quotes.open",
        "high": "quotes.high",
        "low": "quotes.low",
        "close": "quotes.close",
        "vol": "quotes.vol",
        "adj_factor": "quotes.adj_factor",
        "pre_close": "quotes.pre_close",
        # CSV 无精确涨跌停：清洗层置 NULL，撮合回退 limits 板块规则推导
        "up_limit": "quotes.up_limit",
        "down_limit": "quotes.down_limit",
        "calendar_date": "cal.cal_date",
        "dividend_ex_date": "div.ex_date",
        "dividend_stk_div": "div.stk_div",
        "dividend_cash_div": "div.cash_div",
    }


def _build_csv_conn(tmp_path):
    quotes_csv = tmp_path / "quotes.csv"
    quotes_csv.write_text(DIRTY_CSV, encoding="utf-8")
    cal_csv = tmp_path / "cal.csv"
    cal_csv.write_text(CAL_CSV, encoding="utf-8")
    div_csv = tmp_path / "div.csv"
    div_csv.write_text(DIV_CSV, encoding="utf-8")
    conn = duckdb.connect()
    # 清洗层：printf 补零拼后缀、strftime 转 YYYYMMDD、缺失涨跌停 NULL 化
    conn.execute(f"""
    CREATE VIEW quotes AS SELECT
        CASE WHEN code < 100000
             THEN printf('%06d.SZ', code) ELSE printf('%06d.SH', code)
        END AS ts_code,
        strftime(dt, '%Y%m%d') AS trade_date,
        open, high, low, close, vol, adj_factor, pre_close,
        CAST(NULL AS DOUBLE) AS up_limit, CAST(NULL AS DOUBLE) AS down_limit
    FROM read_csv('{quotes_csv}', header=true);
    -- 日历/除息日无横线的纯数字串会被 read_csv 推断成 BIGINT，
    -- 键列必须显式 CAST 回 VARCHAR（键校验会拦 BIGINT 键）
    CREATE VIEW cal AS
        SELECT CAST(cal_date AS VARCHAR) AS cal_date
        FROM read_csv('{cal_csv}', header=true);
    CREATE VIEW div AS SELECT
        printf('%06d.SZ', code) AS ts_code,
        CAST(ex_date AS VARCHAR) AS ex_date, stk_div, cash_div
    FROM read_csv('{div_csv}', header=true);
    """)
    return conn


# ── 测试 ──


def test_conn_and_db_path_mutually_exclusive(tmp_path):
    form = dict(SQLITE_FORM)
    with pytest.raises(ValueError, match="二选一"):
        GenericSQLBackend(form)  # 都不传
    with pytest.raises(ValueError, match="二选一"):
        GenericSQLBackend(form, str(tmp_path / "x.duckdb"), conn=duckdb.connect())


def test_sqlite_attach_three_part_names(tmp_path):
    db = str(tmp_path / "upstream.db")
    _build_sqlite(db)
    conn = duckdb.connect()
    _attach_sqlite_or_skip(conn, db)
    backend = GenericSQLBackend(SQLITE_FORM, conn=conn)
    try:
        # 键外并集拼表：quotes + adj 两表按 (trade_date, ts_code) 对齐
        df = backend.query_bars(None, "20240101", "20240131")
        assert set(df.index.get_level_values("symbol")) == {
            "000001.SZ", "600000.SH",
        }
        row = df.loc[("20240102", "000001.SZ")]
        assert row["close"] == 10.5
        assert row["adj_factor"] == 1.5
        assert backend.get_calendar("20240101", "20240102") == ["20240101", "20240102"]
        assert backend.get_dividends_on_date("20240103") == {
            "000001.SZ": {"stk_div": 0.0, "cash_div": 0.5},
        }
    finally:
        backend.close()
        conn.close()


def test_sqlite_attach_columns_pruned_and_symbols(tmp_path):
    db = str(tmp_path / "upstream.db")
    _build_sqlite(db)
    conn = duckdb.connect()
    _attach_sqlite_or_skip(conn, db)
    backend = GenericSQLBackend(SQLITE_FORM, conn=conn)
    try:
        df = backend.query_bars(["000001.SZ"], "20240101", "20240131",
                                columns=["close"])
        assert list(df.columns) == ["close"]
        assert set(df.index.get_level_values("symbol")) == {"000001.SZ"}
    finally:
        backend.close()
        conn.close()


def test_csv_view_with_cleaning_layer(tmp_path):
    conn = _build_csv_conn(tmp_path)
    backend = GenericSQLBackend(_csv_form(), conn=conn)
    try:
        df = backend.query_bars(None, "20240101", "20240131")
        # 清洗层生效：代码补零带后缀、日期归一 YYYYMMDD
        assert set(df.index.get_level_values("symbol")) == {
            "000001.SZ", "600000.SH",
        }
        assert set(df.index.get_level_values("trade_date")) == {"20240102"}
        row = df.loc[("20240102", "000001.SZ")]
        assert row["close"] == 10.5
        assert row["adj_factor"] == 1.5
        assert pd.isna(row["up_limit"])  # 缺失涨跌停 = NaN，撮合走板块规则回退
        assert backend.get_dividends_on_date("20240103") == {
            "000001.SZ": {"stk_div": 0.0, "cash_div": 0.5},
        }
    finally:
        backend.close()
        conn.close()


def test_dirty_keys_rejected(tmp_path):
    """跳过清洗层直连脏 CSV：键列 DATE/BIGINT 必须被 fail-fast 拒绝，
    而不是静默错算（契约：键列须为 YYYYMMDD 文本 / VARCHAR）。"""
    quotes_csv = tmp_path / "dirty.csv"
    quotes_csv.write_text(DIRTY_CSV, encoding="utf-8")
    cal_csv = tmp_path / "cal.csv"
    cal_csv.write_text(CAL_CSV, encoding="utf-8")
    div_csv = tmp_path / "div.csv"
    div_csv.write_text(DIV_CSV, encoding="utf-8")
    conn = duckdb.connect()
    conn.execute(f"""
    CREATE VIEW dirty AS SELECT code AS ts_code, dt AS trade_date, *
        EXCLUDE (code, dt)
        FROM read_csv('{quotes_csv}', header=true);
    CREATE VIEW dirty_cal AS
        SELECT * FROM read_csv('{cal_csv}', header=true);
    CREATE VIEW dirty_div AS
        SELECT code AS ts_code, ex_date, stk_div, cash_div
        FROM read_csv('{div_csv}', header=true);
    """)
    form = _csv_form()
    col_map = {
        "open": "open", "high": "high", "low": "low", "close": "close",
        "vol": "vol", "adj_factor": "adj_factor", "pre_close": "pre_close",
        "up_limit": "open", "down_limit": "open",
    }
    for key, col in col_map.items():
        form[key] = f"dirty.{col}"
    form["calendar_date"] = "dirty_cal.cal_date"
    form["dividend_ex_date"] = "dirty_div.ex_date"
    form["dividend_stk_div"] = "dirty_div.stk_div"
    form["dividend_cash_div"] = "dirty_div.cash_div"
    with pytest.raises(ValueError, match="键列类型不匹配"):
        GenericSQLBackend(form, conn=conn)
    conn.close()


def test_injected_conn_not_closed_by_backend(tmp_path):
    """注入连接归调用方持有：backend.close() 只注销内部视图，不关连接。"""
    db = str(tmp_path / "upstream.db")
    _build_sqlite(db)
    conn = duckdb.connect()
    _attach_sqlite_or_skip(conn, db)
    backend = GenericSQLBackend(SQLITE_FORM, conn=conn)
    backend.query_bars(["000001.SZ"], "20240101", "20240131")
    backend.close()
    # 连接仍可用（注入方继续持有）；backend 实例自身也不再持有引用语义
    assert conn.execute("SELECT 42").fetchone() == (42,)
    conn.close()


def test_db_path_path_unchanged(tmp_path):
    """db_path 路径行为不变：文件不存在报 FileNotFoundError（既有契约）。"""
    with pytest.raises(FileNotFoundError):
        GenericSQLBackend(SQLITE_FORM, str(tmp_path / "nope.duckdb"))
