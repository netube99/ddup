import sqlite3
from dataclasses import dataclass

import numpy as np

from btcore.database import (
    init_backtest_db,
    read_run_data,
    read_runs,
    update_run_status,
    write_daily,
    write_holdings,
    write_run,
    write_run_stats,
    write_trade,
)
from tests.conftest import make_account, make_holding


@dataclass
class FakeTrade:
    date: str = "20240601"
    symbol: str = "000001.SZ"
    side: str = "BUY"
    trigger: str = "MANUAL"
    price: float = 10.0
    shares: int = 100
    turnover: float = 1000.0
    commission: float = 1.5
    stamp_tax: float = 0.0
    transfer_fee: float = 0.01
    slippage_amount: float = 0.04
    net_amount: float = 1001.55
    reason: str = "MANUAL"


def _write_run(conn, strategy: str = "test") -> int:
    return write_run(conn, created_at="2024-06-01", strategy=strategy,
                     start_date="20240601", end_date="20240605",
                     initial_capital=1000000.0, config_json="{}",
                     status="running")


def test_init_backtest_db():
    conn = init_backtest_db(":memory:")
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    ).fetchall()
    table_names = [t[0] for t in tables]
    assert "runs" in table_names
    assert "account_daily" in table_names
    assert "holdings" in table_names
    assert "trade_log" in table_names
    conn.close()


def test_write_run():
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    assert run_id == 1
    row = conn.execute(
        "SELECT strategy, status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "test"
    assert row[1] == "running"
    conn.close()


def test_write_daily():
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    write_daily(conn, run_id, "20240601", 950000.0, 1000000.0, 500.0, 500.0,
                1000000.0)
    row = conn.execute(
        "SELECT cash, total_value FROM account_daily"
        " WHERE run_id = ? AND date = '20240601'",
        (run_id,),
    ).fetchone()
    assert row[0] == 950000.0
    assert row[1] == 1000000.0
    conn.close()


def test_write_holdings():
    conn = init_backtest_db(":memory:")
    holding = make_holding(shares=100, entry_date="20240601", last_price=10.5,
                           conditions=[{"type": "STOP_LOSS", "price": 9.0}])
    account = make_account(cash=100_000.0, holdings={"000001.SZ": holding})
    write_holdings(conn, account)
    row = conn.execute(
        "SELECT shares FROM holdings WHERE symbol='000001.SZ'"
    ).fetchone()
    assert row[0] == 100
    conn.close()


def test_write_trade():
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    trade = FakeTrade()
    write_trade(conn, run_id, trade)
    row = conn.execute(
        "SELECT run_id, side, price FROM trade_log WHERE symbol='000001.SZ'"
    ).fetchone()
    assert row[0] == run_id
    assert row[1] == "BUY"
    assert row[2] == 10.0
    conn.close()


def test_update_run_status():
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    update_run_status(conn, run_id, "completed")
    row = conn.execute(
        "SELECT status FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    assert row[0] == "completed"
    conn.close()


def test_multi_run_accumulate(tmp_path):
    """两次 init + 写入：runs 累积两行，trade_log 按 run_id 区分并可过滤。"""
    db_path = str(tmp_path / "multi.db")

    conn = init_backtest_db(db_path)
    run1 = _write_run(conn, strategy="s1")
    write_trade(conn, run1, FakeTrade(date="20240601"))
    update_run_status(conn, run1, "completed")
    conn.commit()
    conn.close()

    conn = init_backtest_db(db_path)
    run2 = _write_run(conn, strategy="s2")
    write_trade(conn, run2, FakeTrade(date="20240603"))
    conn.commit()

    assert run1 != run2
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    assert n_runs == 2

    rows = conn.execute(
        "SELECT date FROM trade_log WHERE run_id = ? ORDER BY id", (run1,)
    ).fetchall()
    assert [r[0] for r in rows] == ["20240601"]
    rows = conn.execute(
        "SELECT date FROM trade_log WHERE run_id = ? ORDER BY id", (run2,)
    ).fetchall()
    assert [r[0] for r in rows] == ["20240603"]

    # 第一次 run 的 trade_log 保留（不再整表清空）
    n_trades = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    assert n_trades == 2
    conn.close()


def test_old_schema_rebuilt(tmp_path):
    """旧 schema（runs 无 run_id 列）检测后 DROP 重建为新 schema。"""
    db_path = str(tmp_path / "old.db")

    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE runs (created_at TEXT, strategy TEXT)")
    conn.execute("INSERT INTO runs VALUES ('x', 'old')")
    conn.commit()
    conn.close()

    conn = init_backtest_db(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "run_id" in cols
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
    conn.close()


def test_write_run_stats():
    """stats_json 写入并可读回；numpy 标量经 default 降级为 JSON 数值。"""
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    write_run_stats(conn, run_id, {
        "total_return": np.float64(0.123),
        "trade_count": np.int64(7),
        "nested": {"sharpe": np.float64(1.5)},
    })
    _, _, stats = read_run_data(conn, run_id)
    assert stats["total_return"] == 0.123
    assert stats["trade_count"] == 7
    assert stats["nested"]["sharpe"] == 1.5
    conn.close()


def test_read_run_data_no_stats():
    """未写 stats_json 的 run，stats 返回 None（调用方自行重算）。"""
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    write_daily(conn, run_id, "20240601", 950000.0, 1000000.0, 0.0, 0.0, 1000000.0)
    adf, tdf, stats = read_run_data(conn, run_id)
    assert len(adf) == 1
    assert tdf.empty
    assert stats is None
    runs = read_runs(conn)
    assert list(runs["run_id"]) == [run_id]
    conn.close()


def test_stats_json_migration(tmp_path):
    """老库（runs 有 run_id 无 stats_json）ALTER 迁移，历史行保留为 NULL。"""
    db_path = str(tmp_path / "mig.db")

    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " created_at TEXT, strategy TEXT, status TEXT)"
    )
    conn.execute("INSERT INTO runs (created_at, strategy, status)"
                 " VALUES ('x', 'old', 'completed')")
    conn.commit()
    conn.close()

    conn = init_backtest_db(db_path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(runs)").fetchall()}
    assert "stats_json" in cols
    row = conn.execute("SELECT strategy, stats_json FROM runs").fetchone()
    assert row[0] == "old"
    assert row[1] is None
    conn.close()
