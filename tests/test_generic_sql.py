"""GenericSQLBackend 契约测试：全平铺填表式 SQL 后端。

用临时 sqlite 库验证：ABC 三方法、"表名.字段名" 位置解析、无主表网格
（键外并集，OHLC 刻意拆两张表）、列裁剪、表单校验、filters 编译、
鸭子类型扩展按已填的空装配（未填即不存在）、扩展方法语义。
"""

import logging
import sqlite3

import pandas as pd
import pytest

from btcore.generic_sql import GenericSQLBackend

MINIMAL_FORM = {
    "symbol": "ts_code",        # 通用查询键：纯列名，不带表名
    "date": "trade_date",
    # OHLC 等契约字段刻意拆在两张表里：没有"主表"，各填各的
    "open": "quotes_a.open",
    "high": "quotes_a.high",
    "low": "quotes_b.low",
    "close": "quotes_a.close",
    "vol": "quotes_a.vol",
    "adj_factor": "quotes_a.adj_factor",
    "pre_close": "quotes_a.pre_close",
    "up_limit": "quotes_b.up_limit",
    "down_limit": "quotes_b.down_limit",
    "calendar_date": "cal.cal_date",
    "dividend_ex_date": "div.ex_date",
    "dividend_stk_div": "div.stk_div",
    "dividend_cash_div": "div.cash_div",
}

FULL_FORM = {
    **MINIMAL_FORM,
    "extra_fields": {
        "amount": "quotes_a.amount",      # 扩展字段（非契约必需）
        "pe_ttm": "quotes_a.pe_ttm",     # 同表扩展字段
        "score": "aux.score",            # 第三张表
        "score2": "aux2.score2",         # 键列名不一致的表
    },
    "tables": {
        "aux2": {"symbol": "code", "date": "dt"},      # 键列名覆盖（任意表）
        "cal": {"filter": {"is_open": 1}},             # 行筛选（角色表）
        "div": {"filter": {"note": "实施", "ex_date": None}},   # None → IS NOT NULL
        "st": {"filter": {"type": "ST"}},
    },
    "st_symbol": "st.ts_code",
    "industry_name": "ind.name",
    "listing_date": "listings.list_date",
    "index_code": "idx.index_code",
    "index_member": "idx.con_code",
    "benchmark_close": "bench.close",
    "benchmark_adj_factor": "badj.adj_factor",
    "benchmark_code": "510300.SH",
}


def _build_db(path):
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE quotes_a (ts_code TEXT, trade_date TEXT, open REAL, high REAL,
                           close REAL, vol REAL, amount REAL,
                           adj_factor REAL, pre_close REAL, pe_ttm REAL);
    CREATE TABLE quotes_b (ts_code TEXT, trade_date TEXT, low REAL,
                           up_limit REAL, down_limit REAL);
    CREATE TABLE aux (ts_code TEXT, trade_date TEXT, score REAL);
    CREATE TABLE aux2 (code TEXT, dt TEXT, score2 REAL);
    CREATE TABLE cal (cal_date TEXT, is_open INTEGER);
    CREATE TABLE div (ts_code TEXT, ex_date TEXT, stk_div REAL, cash_div REAL, note TEXT);
    CREATE TABLE st (ts_code TEXT, trade_date TEXT, type TEXT);
    CREATE TABLE ind (ts_code TEXT, name TEXT);
    CREATE TABLE listings (ts_code TEXT, list_date TEXT);
    CREATE TABLE idx (index_code TEXT, con_code TEXT, trade_date TEXT);
    CREATE TABLE bench (ts_code TEXT, trade_date TEXT, close REAL);
    CREATE TABLE badj (ts_code TEXT, trade_date TEXT, adj_factor REAL);
    """)
    conn.executemany(
        "INSERT INTO quotes_a VALUES (?,?,?,?,?,?,?,?,?,?)",
        [("000001.SZ", "20240102", 10.0, 10.8, 10.5, 100.0, 1050.0, 2.0, 10.0, 8.0),
         ("000001.SZ", "20240103", 10.5, 11.0, 11.0, 110.0, 1210.0, 2.0, 10.5, 8.1),
         ("600000.SH", "20240102", 20.0, 20.6, 20.5, 200.0, 4100.0, 1.5, 20.0, 6.0)])
    conn.executemany(
        "INSERT INTO quotes_b VALUES (?,?,?,?,?)",
        [("000001.SZ", "20240102", 9.9, 11.0, 9.0),
         ("000001.SZ", "20240103", 10.4, 11.55, 9.45),
         # 仅存在于 quotes_b 的行：quotes_a 无此 (date, symbol)
         ("600000.SH", "20240103", 20.1, 22.0, 18.0)])
    conn.execute("INSERT INTO aux VALUES ('000001.SZ', '20240102', 0.9)")
    conn.execute("INSERT INTO aux2 VALUES ('000001.SZ', '20240102', 7.7)")
    conn.executemany("INSERT INTO cal VALUES (?,?)",
                     [("20240102", 1), ("20240103", 1), ("20240104", 0)])
    conn.executemany(
        "INSERT INTO div VALUES (?,?,?,?,?)",
        [("000001.SZ", "20240103", 0.5, 1.0, "实施"),
         ("600000.SH", "20240103", None, 0.3, "实施"),
         ("300750.SZ", "20240103", 0.2, 0.2, "预案"),
         ("399001.SZ", None, 0.1, 0.1, "实施")])   # ex_date 为 NULL，应被 filter 排除
    conn.executemany("INSERT INTO st VALUES (?,?,?)",
                     [("000001.SZ", "20240101", "ST"),
                      ("600000.SH", "20240105", "ST"),
                      ("300750.SZ", "20240101", "PT")])
    conn.execute("INSERT INTO ind VALUES ('000001.SZ', '银行')")
    conn.executemany("INSERT INTO listings VALUES (?,?)",
                     [("000001.SZ", "20231201"), ("600000.SH", "20240101")])
    conn.executemany("INSERT INTO idx VALUES (?,?,?)",
                     [("000300.SH", "000001.SZ", "20231229"),
                      ("000300.SH", "600000.SH", "20231229"),
                      ("000905.SH", "300750.SZ", "20231229")])
    conn.executemany("INSERT INTO bench VALUES (?,?,?)",
                     [("510300.SH", "20240102", 10.0),
                      ("510300.SH", "20240103", 11.0),
                      ("000905.SH", "20240102", 5.0)])
    conn.executemany("INSERT INTO badj VALUES (?,?,?)",
                     [("510300.SH", "20240102", 2.0),
                      ("510300.SH", "20240103", 2.2)])
    conn.commit()
    conn.close()


@pytest.fixture
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    _build_db(path)
    return path


@pytest.fixture
def backend(db_path):
    b = GenericSQLBackend(FULL_FORM, db_path)
    yield b
    b.close()


# ── 表单校验 ──


def test_missing_required_blank(db_path):
    form = {k: v for k, v in MINIMAL_FORM.items() if k != "vol"}
    with pytest.raises(ValueError, match="vol"):
        GenericSQLBackend(form, db_path)


def test_location_needs_dot(db_path):
    form = {**MINIMAL_FORM, "open": "open"}   # 裸列名不再接受
    with pytest.raises(ValueError, match="表名.字段名"):
        GenericSQLBackend(form, db_path)


def test_key_blanks_bare_name(db_path):
    form = {**MINIMAL_FORM, "symbol": "quotes_a.ts_code"}   # 键列只写列名
    with pytest.raises(ValueError, match="列名"):
        GenericSQLBackend(form, db_path)


def test_unknown_top_key(db_path):
    with pytest.raises(ValueError, match="未知键"):
        GenericSQLBackend({**MINIMAL_FORM, "bar_table": "quotes_a"}, db_path)


def test_unknown_table_rejected(db_path):
    form = {**MINIMAL_FORM, "st_symbol": "no_such_table.ts_code"}
    with pytest.raises(ValueError, match="不存在"):
        GenericSQLBackend(form, db_path)


def test_unknown_column_rejected(db_path):
    form = {**MINIMAL_FORM, "open": "quotes_a.no_such_col"}
    with pytest.raises(ValueError, match=r"quotes_a\.no_such_col"):
        GenericSQLBackend(form, db_path)


def test_pair_completeness(db_path):
    form = {**MINIMAL_FORM, "index_code": "idx.index_code"}
    with pytest.raises(ValueError, match="成对"):
        GenericSQLBackend(form, db_path)
    form2 = {**MINIMAL_FORM, "benchmark_adj_factor": "badj.adj_factor"}
    with pytest.raises(ValueError, match="benchmark_close"):
        GenericSQLBackend(form2, db_path)


def test_benchmark_close_only(db_path):
    """benchmark_adj_factor 可选：只填 close 时 hfq_close 即原始 close。"""
    form = {k: v for k, v in FULL_FORM.items() if k != "benchmark_adj_factor"}
    b = GenericSQLBackend(form, db_path)
    try:
        df = b.get_benchmark_bars("510300.SH", "20240102", "20240103")
        assert df["hfq_close"].tolist() == [10.0, 11.0]
    finally:
        b.close()


def test_pair_same_table(db_path):
    form = {**MINIMAL_FORM, "index_code": "idx.index_code",
            "index_member": "aux.con_code"}
    with pytest.raises(ValueError, match="同一张表"):
        GenericSQLBackend(form, db_path)


def test_dividends_same_table(db_path):
    form = {**MINIMAL_FORM, "dividend_cash_div": "aux.cash_div"}
    with pytest.raises(ValueError, match="同一张表"):
        GenericSQLBackend(form, db_path)


def test_extra_fields_reserved(db_path):
    form = {**MINIMAL_FORM, "extra_fields": {"pct_chg": "quotes_a.close"}}
    with pytest.raises(ValueError, match="保留名"):
        GenericSQLBackend(form, db_path)


def test_extra_fields_fixed_vocabulary(db_path):
    form = {**MINIMAL_FORM, "extra_fields": {"open": "quotes_a.open"}}
    with pytest.raises(ValueError, match="保留名"):
        GenericSQLBackend(form, db_path)


def test_tables_stray_table(db_path):
    form = {**MINIMAL_FORM, "tables": {"no_such": {"filter": {"a": 1}}}}
    with pytest.raises(ValueError, match="未被任何字段引用"):
        GenericSQLBackend(form, db_path)


def test_tables_unknown_option_key(db_path):
    form = {**FULL_FORM, "tables": {**FULL_FORM["tables"], "aux2": {"foo": 1}}}
    with pytest.raises(ValueError, match="未知键"):
        GenericSQLBackend(form, db_path)


def test_filter_only_on_role_tables(db_path):
    form = {**MINIMAL_FORM, "tables": {"quotes_a": {"filter": {"a": 1}}}}
    with pytest.raises(ValueError, match="只对日历/分红/ST/指数成分表有效"):
        GenericSQLBackend(form, db_path)


# ── 鸭子类型装配 ──


def test_extras_absent_when_unconfigured(db_path):
    b = GenericSQLBackend(MINIMAL_FORM, db_path)
    for m in ("get_benchmark_bars", "get_st_symbols", "get_st_map",
              "get_stock_industries", "get_recent_listings", "get_index_members"):
        assert getattr(b, m, None) is None
        assert not hasattr(b, m)
    b.close()


def test_extras_present_when_configured(backend):
    for m in ("get_benchmark_bars", "get_st_symbols", "get_st_map",
              "get_stock_industries", "get_recent_listings", "get_index_members"):
        assert callable(getattr(backend, m))


def test_subclass_override_wins(db_path):
    class Custom(GenericSQLBackend):
        def get_benchmark_bars(self, code=None, start="", end=""):
            return "custom"

    b = Custom(FULL_FORM, db_path)
    assert b.get_benchmark_bars() == "custom"
    b.close()


# ── ABC 方法 ──


def test_query_bars_full(backend):
    df = backend.query_bars(["000001.SZ"], "20240102", "20240103")
    assert list(df.index.names) == ["trade_date", "symbol"]
    assert len(df) == 2
    # 拆在两张表的契约字段 + 第三张表 + 键列覆盖表
    for col in ("open", "close", "low", "pe_ttm", "up_limit", "down_limit",
                "score", "score2"):
        assert col in df.columns
    row = df.loc[("20240102", "000001.SZ")]
    assert row["up_limit"] == 11.0
    assert row["low"] == 9.9
    assert row["score"] == 0.9
    assert row["score2"] == 7.7   # table_keys 覆盖的表按 dt/code 正确对齐


def test_query_bars_outer_union(backend):
    """无主表：仅存在于单张表的 (date, symbol) 行也进网格，其余列 NaN。"""
    df = backend.query_bars(["600000.SH"], "20240102", "20240103")
    assert len(df) == 2   # 0102 仅在 quotes_a，0103 仅在 quotes_b
    row_a = df.loc[("20240102", "600000.SH")]
    assert row_a["close"] == 20.5
    assert pd.isna(row_a["low"]) and pd.isna(row_a["up_limit"])
    row_b = df.loc[("20240103", "600000.SH")]
    assert pd.isna(row_b["close"]) and pd.isna(row_b["open"])
    assert row_b["low"] == 20.1 and row_b["up_limit"] == 22.0


def test_query_bars_column_pruning(backend):
    df = backend.query_bars(["000001.SZ"], "20240102", "20240103",
                            ["close", "down_limit", "score"])
    # 只返回请求列；无字段被请求的表（aux2）不出现在结果中
    assert sorted(df.columns) == ["close", "down_limit", "score"]


def test_query_bars_unknown_column(backend):
    with pytest.raises(ValueError, match="未知列名"):
        backend.query_bars(["000001.SZ"], "20240102", "20240103", ["no_such_col"])


def test_reserved_word_columns(db_path):
    """物理表/列名撞 SQL 保留字（limit/order）：标识符加引号后照常对接。"""
    conn = sqlite3.connect(db_path)
    conn.execute('CREATE TABLE ev (ts_code TEXT, trade_date TEXT,'
                 ' "limit" TEXT, "order" REAL)')
    conn.execute("INSERT INTO ev VALUES ('000001.SZ', '20240102', 'U', 3.5)")
    conn.commit()
    conn.close()
    form = {**MINIMAL_FORM,
            "extra_fields": {"limit_flag": "ev.limit", "ord": "ev.order"}}
    b = GenericSQLBackend(form, db_path)
    try:
        df = b.query_bars(["000001.SZ"], "20240101", "20240105",
                          ["close", "limit_flag", "ord"])
        row = df.loc[("20240102", "000001.SZ")]
        assert row["limit_flag"] == "U"
        assert row["ord"] == 3.5
        assert row["close"] == 10.5
    finally:
        b.close()


def test_get_calendar_filter(backend):
    assert backend.get_calendar("20240101", "20240131") == ["20240102", "20240103"]


def test_get_dividends(backend):
    div = backend.get_dividends_on_date("20240103")
    # filters 排除 "预案" 与 ex_date 为 NULL 的行；NULL 字段回落 0.0
    assert div == {"000001.SZ": {"stk_div": 0.5, "cash_div": 1.0},
                   "600000.SH": {"stk_div": 0.0, "cash_div": 0.3}}
    assert backend.get_dividends_on_date("20240102") == {}


# ── 扩展方法 ──


def test_st(backend):
    """ST 表是日频快照：get_st_symbols 返回当日名单，摘帽次日即不在名单。"""
    assert backend.get_st_symbols("20240101") == {"000001.SZ"}
    assert backend.get_st_symbols("20240103") == set()
    assert backend.get_st_symbols("20240105") == {"600000.SH"}
    assert backend.get_st_map("20240102") == {"20240105": {"600000.SH"}}


def test_industries(backend):
    assert backend.get_stock_industries(["000001.SZ"]) == {"000001.SZ": "银行"}
    assert backend.get_stock_industries([]) == {}


def test_recent_listings(backend):
    assert backend.get_recent_listings(60, "20240110") == {"000001.SZ", "600000.SH"}
    assert backend.get_recent_listings(30, "20240110") == {"600000.SH"}


def test_index_members(backend):
    members = backend.get_index_members(["000300.SH", "000905.SH"],
                                        "20231201", "20240131")
    assert members == {"20231229": {"000001.SZ", "600000.SH", "300750.SZ"}}
    assert backend.get_index_members([], "20240101", "20240131") == {}


def test_benchmark_hfq_anchor(backend):
    df = backend.get_benchmark_bars("510300.SH", "20240102", "20240103")
    # hfq 锚定首日：close * adj / first_adj = 10*2/2, 11*2.2/2
    assert df["hfq_close"].tolist() == pytest.approx([10.0, 12.1])
    assert isinstance(df.index, pd.DatetimeIndex)


def test_benchmark_default_code(backend):
    df = backend.get_benchmark_bars(None, "20240102", "20240102")
    assert df["hfq_close"].tolist() == [10.0]


def test_benchmark_empty_adj_falls_back(backend, caplog):
    with caplog.at_level(logging.WARNING):
        df = backend.get_benchmark_bars("000905.SH", "20240102", "20240102")
    # 无复权因子 → 退化为未复权 close 并告警
    assert df["hfq_close"].tolist() == [5.0]
    assert "退化为未复权" in caplog.text


def test_benchmark_no_bars_returns_none(backend):
    assert backend.get_benchmark_bars("999999.XX", "20240102", "20240103") is None


def test_view_as_source_table(tmp_path):
    """任何被引用的"表"都可以是 VIEW：复杂拼接逻辑在库里一次性表达，
    表单无需感知。"""
    path = str(tmp_path / "view.db")
    conn = sqlite3.connect(path)
    conn.executescript("""
    CREATE TABLE daily (ts_code TEXT, trade_date TEXT, open REAL, high REAL,
                        low REAL, close REAL, vol REAL, amount REAL,
                        pre_close REAL, up_limit REAL, down_limit REAL);
    CREATE TABLE adj (ts_code TEXT, trade_date TEXT, adj_factor REAL);
    CREATE TABLE cal (cal_date TEXT);
    CREATE TABLE div (ts_code TEXT, ex_date TEXT, stk_div REAL, cash_div REAL);
    CREATE VIEW v_bars AS
        SELECT d.*, a.adj_factor
        FROM daily d
        LEFT JOIN adj a ON a.ts_code=d.ts_code AND a.trade_date=d.trade_date;
    """)
    conn.execute("INSERT INTO daily VALUES ('000001.SZ','20240102',10.0,10.8,9.9,"
                 "10.5,100.0,1050.0,10.0,11.0,9.0)")
    conn.execute("INSERT INTO adj VALUES ('000001.SZ','20240102',1.25)")
    conn.commit()
    conn.close()

    form = {"symbol": "ts_code", "date": "trade_date",
            **{f: f"v_bars.{f}" for f in ("open", "high", "low", "close", "vol",
                                          "adj_factor", "pre_close",
                                          "up_limit", "down_limit")},
            "extra_fields": {"amount": "v_bars.amount"},
            "calendar_date": "cal.cal_date",
            "dividend_ex_date": "div.ex_date",
            "dividend_stk_div": "div.stk_div",
            "dividend_cash_div": "div.cash_div"}
    b = GenericSQLBackend(form, path)
    df = b.query_bars(["000001.SZ"], "20240102", "20240102")
    assert df.loc[("20240102", "000001.SZ"), "adj_factor"] == 1.25
    # 列裁剪对 VIEW 同样生效（pragma 探测 VIEW 列）
    df2 = b.query_bars(["000001.SZ"], "20240102", "20240102", ["adj_factor"])
    assert list(df2.columns) == ["adj_factor"]
    b.close()
