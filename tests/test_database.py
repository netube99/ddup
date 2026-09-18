from dataclasses import dataclass

import numpy as np

from btcore.database import (
    init_backtest_db,
    read_run_data,
    read_runs,
    transaction,
    update_run_status,
    write_daily,
    write_holdings,
    write_run,
    write_run_stats,
    write_trade,
    write_trades,
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
    tables = {
        r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()
    }
    assert {"runs", "account_daily", "holdings", "trade_log",
            "debug_snapshots", "ml_predictions"} <= tables
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


def test_run_id_sequence_increments():
    """DuckDB 无 AUTOINCREMENT：run_id 由 sequence 自增且跨重开延续。"""
    conn = init_backtest_db(":memory:")
    assert _write_run(conn) == 1
    assert _write_run(conn) == 2
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


def test_write_trades_batch():
    """批量写入：executemany 一次落多笔，id 按序自增。"""
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    trades = [FakeTrade(date="20240601"), FakeTrade(date="20240602", side="SELL")]
    write_trades(conn, run_id, trades)
    rows = conn.execute(
        "SELECT id, date, side FROM trade_log ORDER BY id"
    ).fetchall()
    assert [(r[1], r[2]) for r in rows] == [
        ("20240601", "BUY"), ("20240602", "SELL"),
    ]
    assert rows[0][0] < rows[1][0]
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


def test_transaction_rollback():
    """显式事务：异常回滚不落库，成功提交可见。"""
    conn = init_backtest_db(":memory:")
    run_id = _write_run(conn)
    try:
        with transaction(conn):
            write_daily(conn, run_id, "20240601", 1.0, 1.0, 0.0, 0.0, 1.0)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    assert conn.execute("SELECT COUNT(*) FROM account_daily").fetchone()[0] == 0
    with transaction(conn):
        write_daily(conn, run_id, "20240602", 1.0, 1.0, 0.0, 0.0, 1.0)
    assert conn.execute("SELECT COUNT(*) FROM account_daily").fetchone()[0] == 1
    conn.close()


def test_multi_run_accumulate(tmp_path):
    """两次 init + 写入：runs 累积两行，trade_log 按 run_id 区分并可过滤。"""
    db_path = str(tmp_path / "multi.duckdb")

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
