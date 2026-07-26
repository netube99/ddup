import datetime
import json
import sqlite3

import pandas as pd

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    strategy TEXT,
    start_date TEXT,
    end_date TEXT,
    initial_capital REAL,
    config_json TEXT,
    status TEXT,
    stats_json TEXT
);

CREATE TABLE IF NOT EXISTS account_daily (
    run_id         INTEGER NOT NULL,
    date           TEXT NOT NULL,
    cash           REAL NOT NULL,
    total_value    REAL NOT NULL,
    daily_pnl      REAL NOT NULL DEFAULT 0,
    cumulative_pnl REAL NOT NULL DEFAULT 0,
    initial_capital REAL NOT NULL,
    n_holdings     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, date)
);
CREATE INDEX IF NOT EXISTS idx_account_daily_date ON account_daily(date);

CREATE TABLE IF NOT EXISTS holdings (
    symbol        TEXT PRIMARY KEY,
    entry_date    TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    shares        INTEGER NOT NULL,
    cost          REAL NOT NULL,
    conditions_json TEXT NOT NULL DEFAULT '[]',
    last_price    REAL NOT NULL DEFAULT 0,
    holding_days  INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS trade_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL,
    date          TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    side          TEXT NOT NULL,
    trigger       TEXT NOT NULL,
    price         REAL NOT NULL,
    shares        INTEGER NOT NULL,
    turnover      REAL NOT NULL,
    commission    REAL NOT NULL,
    stamp_tax     REAL NOT NULL DEFAULT 0,
    transfer_fee  REAL NOT NULL DEFAULT 0,
    slippage_amount REAL NOT NULL DEFAULT 0,
    net_amount    REAL NOT NULL DEFAULT 0,
    reason        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trade_log_date ON trade_log(date);
CREATE INDEX IF NOT EXISTS idx_trade_log_symbol ON trade_log(symbol);
CREATE INDEX IF NOT EXISTS idx_trade_log_run ON trade_log(run_id);
"""

_ALL_TABLES = ("runs", "account_daily", "holdings", "trade_log")


def init_backtest_db(path: str) -> sqlite3.Connection:
    """初始化多 run 回测库（runs/account_daily/trade_log 按 run_id 累积）。

    同一 path 重复使用时，历史 run 保留，本次写入挂在新 run_id 下；
    holdings 是瞬态快照表，每次 run 开始清空。
    检测到旧 schema（runs 无 run_id 列）时 DROP 全部四表重建——
    旧行为本来就是每 run 清空，丢弃旧库无回归。
    runs 缺 stats_json 列的老库走 ALTER TABLE 轻量迁移，历史 run 保留。
    """
    conn = sqlite3.connect(path)
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    if cols and "run_id" not in cols:
        for table in _ALL_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        cols = set()
    needs_stats_json = bool(cols) and "stats_json" not in cols
    conn.executescript(SCHEMA_SQL)
    if needs_stats_json:
        # 轻量迁移：老库补 stats_json 列，历史 run 保留（stats_json 为 NULL）
        conn.execute("ALTER TABLE runs ADD COLUMN stats_json TEXT")
    conn.execute("DELETE FROM holdings")
    return conn


def write_run(conn: sqlite3.Connection, **kwargs) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (created_at, strategy, start_date, end_date,"
        " initial_capital, config_json, status) VALUES ("
        ":created_at, :strategy, :start_date, :end_date,"
        " :initial_capital, :config_json, :status)",
        kwargs,
    )
    return cursor.lastrowid


def write_daily(conn: sqlite3.Connection, run_id: int, date: str, cash: float,
                total_value: float, daily_pnl: float, cumulative_pnl: float,
                initial_capital: float, n_holdings: int = 0):
    conn.execute(
        "INSERT OR REPLACE INTO account_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, date, cash, total_value, daily_pnl, cumulative_pnl,
         initial_capital, n_holdings),
    )


def write_holdings(conn: sqlite3.Connection, account):
    conn.execute("DELETE FROM holdings")
    for symbol, holding in account.holdings.items():
        conn.execute(
            "INSERT INTO holdings VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                symbol,
                holding.entry_date,
                holding.entry_price,
                holding.shares,
                holding.cost,
                json.dumps(holding.conditions, default=str),
                holding.last_price,
                holding.holding_days,
                datetime.datetime.now().isoformat(),
            ),
        )


def write_trade(conn: sqlite3.Connection, run_id: int, trade):
    conn.execute(
        "INSERT INTO trade_log (run_id, date, symbol, side, trigger, price,"
        " shares, turnover, commission, stamp_tax, transfer_fee,"
        " slippage_amount, net_amount, reason)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            trade.date,
            trade.symbol,
            trade.side,
            trade.trigger,
            trade.price,
            trade.shares,
            trade.turnover,
            trade.commission,
            trade.stamp_tax,
            trade.transfer_fee,
            trade.slippage_amount,
            trade.net_amount,
            trade.reason,
        ),
    )


def update_run_status(conn: sqlite3.Connection, run_id: int, status: str):
    conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))


def _json_default(obj):
    # numpy 标量/pd.Timestamp 等经 item()/str() 降级为 JSON 可序列化值
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def write_run_stats(conn: sqlite3.Connection, run_id: int, stats: dict):
    """把 statistics dict 以 JSON 形式挂到 runs.stats_json，供多 run 对比。"""
    conn.execute(
        "UPDATE runs SET stats_json = ? WHERE run_id = ?",
        (json.dumps(stats, default=_json_default), run_id),
    )


def read_runs(conn: sqlite3.Connection) -> pd.DataFrame:
    """runs 全表（按 run_id 升序），多 run 对比的入口。"""
    return pd.read_sql_query("SELECT * FROM runs ORDER BY run_id", conn)


def read_run_data(conn: sqlite3.Connection, run_id: int):
    """读取单个 run 的 (account_daily, trade_log, stats_dict|None)。

    stats_json 为 NULL（老库历史 run）时 stats_dict 返回 None，调用方自行重算。
    """
    account_daily = pd.read_sql_query(
        "SELECT * FROM account_daily WHERE run_id = ? ORDER BY date",
        conn, params=(run_id,),
    )
    trade_log = pd.read_sql_query(
        "SELECT * FROM trade_log WHERE run_id = ? ORDER BY date, id",
        conn, params=(run_id,),
    )
    row = conn.execute(
        "SELECT stats_json FROM runs WHERE run_id = ?", (run_id,)
    ).fetchone()
    stats = json.loads(row[0]) if row and row[0] else None
    return account_daily, trade_log, stats
