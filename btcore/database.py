import datetime
import json
import sqlite3

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS runs (
    run_id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    strategy TEXT,
    start_date TEXT,
    end_date TEXT,
    initial_capital REAL,
    config_json TEXT,
    status TEXT
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
    """
    conn = sqlite3.connect(path)
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(runs)").fetchall()
    }
    if cols and "run_id" not in cols:
        for table in _ALL_TABLES:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(SCHEMA_SQL)
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
