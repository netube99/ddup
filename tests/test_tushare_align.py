"""事件表日频对齐层回归（F-FAC-06）。

adapters/tushare 的 ddup_*_daily 对齐 VIEW：公告日（ann_date）映射到
下一开市日、同股同日合并，消除非交易日行混入键外并集面板导致的
ts 因子窗口杀值。用小 fixture 库走 open_aligned_conn + 注入连接的
GenericSQLBackend 全链路（与 TushareBackend 同一构造路径）。
"""

import duckdb
import pytest

try:
    from adapters.tushare import open_aligned_conn
except ImportError:
    pytest.skip(
        "adapters/tushare.py 未配置（请从 .template 复制），跳过对齐层测试",
        allow_module_level=True,
    )

from btcore.factors.ops import eval_op_expr
from btcore.generic_sql import GenericSQLBackend

# 开市日：02-05（周二-五）、08-10（周一-三）；06/07 周末非开市
CAL = [
    ("20240101", 0), ("20240102", 1), ("20240103", 1), ("20240104", 1),
    ("20240105", 1), ("20240106", 0), ("20240107", 0), ("20240108", 1),
    ("20240109", 1), ("20240110", 1),
]
OPEN_DAYS = [d for d, o in CAL if o]
QUOTE_DAYS = OPEN_DAYS  # 行情行覆盖全部开市日


def _build_db(path):
    conn = duckdb.connect(path)
    for stmt in """
    CREATE TABLE trade_cal (exchange VARCHAR, cal_date VARCHAR, is_open INT);
    CREATE TABLE quotes (ts_code VARCHAR, trade_date VARCHAR, open DOUBLE,
                         high DOUBLE, low DOUBLE, close DOUBLE, vol DOUBLE,
                         adj_factor DOUBLE, pre_close DOUBLE,
                         up_limit DOUBLE, down_limit DOUBLE);
    CREATE TABLE dividend (ts_code VARCHAR, ex_date VARCHAR,
                           stk_div DOUBLE, cash_div DOUBLE);
    CREATE TABLE repurchase (ts_code VARCHAR, ann_date VARCHAR,
                             vol DOUBLE, amount DOUBLE);
    CREATE TABLE stk_holdertrade (ts_code VARCHAR, ann_date VARCHAR,
                                  in_de VARCHAR, change_vol DOUBLE,
                                  change_ratio DOUBLE);
    CREATE TABLE stk_holdernumber (ts_code VARCHAR, ann_date VARCHAR,
                                   holder_num DOUBLE);
    CREATE TABLE pledge_detail (ts_code VARCHAR, ann_date VARCHAR,
                                p_total_ratio DOUBLE, h_total_ratio DOUBLE);
    """.strip().split(";"):
        if stmt.strip():
            conn.execute(stmt)
    conn.executemany("INSERT INTO trade_cal VALUES ('SSE', ?, ?)", CAL)
    # 000001.SZ 全开市日行情；002550.SZ 只有部分行（复牌前缺行情的开市日）
    for i, d in enumerate(QUOTE_DAYS):
        conn.execute("INSERT INTO quotes VALUES ('000001.SZ', ?, 10, 11, 9,"
                     " 10.5, 1000, 1.0, 10.5, 11.0, 9.0)", [d])
        if i >= 2:
            conn.execute("INSERT INTO quotes VALUES ('002550.SZ', ?, 6, 6.5,"
                         " 5.8, 6.0, 2000, 1.0, 6.0, 6.5, 5.5)", [d])
    # 002550.SZ：周六公告（应对齐到周一 20240108）+ 周四公告（原日可见）
    conn.execute("INSERT INTO repurchase VALUES"
                 " ('002550.SZ', '20240106', 1000, 39401910.0)")
    conn.execute("INSERT INTO repurchase VALUES"
                 " ('002550.SZ', '20240104', 500, 170000000.0)")
    # 000001.SZ：周六两笔公告（对齐同日 → amount 求和）
    conn.execute("INSERT INTO repurchase VALUES"
                 " ('000001.SZ', '20240106', 100, 1.0)")
    conn.execute("INSERT INTO repurchase VALUES"
                 " ('000001.SZ', '20240107', 300, 2.0)")
    # 增减持（带符号口径：DE 减持为负，上游 defect-3 同源）：
    # 周六增持 100@0.01 + 周日减持 300@0.02 → 净额 -200、加权比例
    # (0.01*100 - 0.02*300)/400 = -0.0125（分母为绝对股数之和）
    conn.execute("INSERT INTO stk_holdertrade VALUES"
                 " ('000001.SZ', '20240106', 'IN', 100, 0.01)")
    conn.execute("INSERT INTO stk_holdertrade VALUES"
                 " ('000001.SZ', '20240107', 'DE', 300, 0.02)")
    # 股东人数：同日多公告取 MAX
    conn.execute("INSERT INTO stk_holdernumber VALUES"
                 " ('000001.SZ', '20240106', 80000)")
    conn.execute("INSERT INTO stk_holdernumber VALUES"
                 " ('000001.SZ', '20240107', 90000)")
    # 质押：同日多笔取 MAX
    conn.execute("INSERT INTO pledge_detail VALUES"
                 " ('000001.SZ', '20240106', 5.0, 10.0)")
    conn.execute("INSERT INTO pledge_detail VALUES"
                 " ('000001.SZ', '20240107', 7.0, 6.0)")
    conn.execute("INSERT INTO dividend VALUES"
                 " ('000001.SZ', '20240108', 0.0, 0.1)")
    conn.commit()
    conn.close()


@pytest.fixture
def backend(tmp_path):
    db = str(tmp_path / "mini.duckdb")
    _build_db(db)
    conn = open_aligned_conn(db)
    b = GenericSQLBackend(_MINI_FORM, conn=conn)
    yield b
    b.close()
    conn.close()


_MINI_FORM = {
    "symbol": "ts_code",
    "date": "trade_date",
    "open": "quotes.open",
    "high": "quotes.high",
    "low": "quotes.low",
    "close": "quotes.close",
    "vol": "quotes.vol",
    "adj_factor": "quotes.adj_factor",
    "pre_close": "quotes.pre_close",
    "up_limit": "quotes.up_limit",
    "down_limit": "quotes.down_limit",
    "calendar_date": "trade_cal.cal_date",
    "dividend_ex_date": "dividend.ex_date",
    "dividend_stk_div": "dividend.stk_div",
    "dividend_cash_div": "dividend.cash_div",
    "tables": {"trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}}},
    "extra_fields": {
        "rep_amount": "memory.ddup_repurchase_daily.amount",
        "insider_chg_vol": "memory.ddup_holdertrade_daily.change_vol",
        "insider_chg_ratio": "memory.ddup_holdertrade_daily.change_ratio",
        "holder_num": "memory.ddup_holdernumber_daily.holder_num",
        "pledge_p_total_ratio": "memory.ddup_pledge_daily.p_total_ratio",
        "pledge_h_total_ratio": "memory.ddup_pledge_daily.h_total_ratio",
    },
}


def test_panel_dates_all_trading_days(backend):
    """F-FAC-06 根因消除：请求事件列的面板不出现非交易日行。"""
    df = backend.query_bars(None, "20240101", "20240110")
    dates = set(df.index.get_level_values("trade_date"))
    assert dates <= set(OPEN_DAYS)
    assert "20240106" not in dates and "20240107" not in dates


def test_weekend_announcement_aligned_to_next_open(backend):
    """周六公告的金额在周一面板行可见，事件值不因对齐丢失。"""
    df = backend.query_bars(["002550.SZ"], "20240101", "20240110",
                            columns=["close", "rep_amount"])
    assert df.loc[("20240108", "002550.SZ"), "rep_amount"] == 39401910.0
    assert df.loc[("20240104", "002550.SZ"), "rep_amount"] == 170000000.0
    # 对齐日之外无泄漏
    assert df.loc[("20240109", "002550.SZ"), "rep_amount"] != 39401910.0


def test_same_day_merge_semantics(backend):
    """周六+周日两笔公告合并到周一：回购求和、增减持带符号净额+加权、人数/质押取 MAX。"""
    df = backend.query_bars(["000001.SZ"], "20240101", "20240110")
    row = df.loc[("20240108", "000001.SZ")]
    assert row["rep_amount"] == 3.0                    # 1.0 + 2.0
    assert row["insider_chg_vol"] == -200.0            # +100 - 300（DE 带符号）
    assert row["insider_chg_ratio"] == pytest.approx(-0.0125)
    assert row["holder_num"] == 90000                  # MAX(80000, 90000)
    assert row["pledge_p_total_ratio"] == 7.0          # MAX(5, 7)
    assert row["pledge_h_total_ratio"] == 10.0         # MAX(10, 6)


def test_ts_window_not_killed_by_event_rows(backend):
    """F-FAC-06 杀值消除：请求事件列后 ma(close,3) 不再被公告行杀窗。"""
    df = backend.query_bars(["000001.SZ"], "20240101", "20240110",
                            columns=["close", "rep_amount"])
    ma3 = eval_op_expr(df, "ma(close, 3)")
    # 修复前：周六公告行混入面板 → close=NaN 行 → 20240102 起整窗 NaN
    # 修复后：20240104（第 3 个开市日）起满窗有值
    assert ma3.loc[("20240104", "000001.SZ")] == pytest.approx(10.5)
    assert ma3.loc[("20240108", "000001.SZ")] == pytest.approx(10.5)
    assert ma3.loc[("20240102", "000001.SZ")] != ma3.loc[("20240102", "000001.SZ")]  # NaN


def test_no_duplicate_keys_end_to_end(backend):
    """对齐后 (ts_code, trade_date) 唯一：query_bars 的重复键 fail-fast 不触发。"""
    df = backend.query_bars(None, "20240101", "20240110")
    assert not df.index.duplicated().any()


def test_calendar_and_dividends_unchanged(backend):
    """对齐层只影响四张事件表：日历与分红口径不变。"""
    assert backend.get_calendar("20240101", "20240110") == OPEN_DAYS
    assert backend.get_dividends_on_date("20240108") == {
        "000001.SZ": {"stk_div": 0.0, "cash_div": 0.1},
    }
