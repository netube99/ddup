import datetime
import json
import math
import os
from contextlib import contextmanager

import duckdb
import pandas as pd

# DuckDB 主文件魔数位于 offset 8（前 8 字节为存储版本哈希）
_DUCKDB_MAGIC = b"DUCK"
_DUCKDB_MAGIC_OFFSET = 8


def assert_duckdb_file(path: str, kind: str = "数据库") -> None:
    """旧格式 fail-fast：SQLite 等非 DuckDB 文件不再支持（升级不保留兼容）。

    不拦截时 DuckDB 会经 sqlite_scanner 静默打开旧 SQLite 文件，产生
    不可预期的读写语义；此处显式报错并给出重建指引。
    """
    if not path or path == ":memory:" or not os.path.exists(path):
        return
    with open(path, "rb") as f:
        header = f.read(_DUCKDB_MAGIC_OFFSET + len(_DUCKDB_MAGIC))
    if header[_DUCKDB_MAGIC_OFFSET:_DUCKDB_MAGIC_OFFSET + len(_DUCKDB_MAGIC)] != _DUCKDB_MAGIC:
        raise ValueError(
            f"{kind} {path} 不是 DuckDB 格式（SQLite 等旧格式已不再支持）："
            "请删除旧文件后用当前 CLI 重新生成"
        )

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS runs_run_id_seq;
CREATE TABLE IF NOT EXISTS runs (
    run_id BIGINT PRIMARY KEY DEFAULT nextval('runs_run_id_seq'),
    created_at VARCHAR,
    strategy VARCHAR,
    start_date VARCHAR,
    end_date VARCHAR,
    initial_capital DOUBLE,
    config_json VARCHAR,
    status VARCHAR,
    stats_json VARCHAR
);

CREATE TABLE IF NOT EXISTS account_daily (
    run_id         BIGINT NOT NULL,
    date           VARCHAR NOT NULL,
    cash           DOUBLE NOT NULL,
    total_value    DOUBLE NOT NULL,
    daily_pnl      DOUBLE NOT NULL DEFAULT 0,
    cumulative_pnl DOUBLE NOT NULL DEFAULT 0,
    initial_capital DOUBLE NOT NULL,
    n_holdings     BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, date)
);
CREATE INDEX IF NOT EXISTS idx_account_daily_date ON account_daily(date);

CREATE TABLE IF NOT EXISTS holdings (
    symbol        VARCHAR PRIMARY KEY,
    entry_date    VARCHAR NOT NULL,
    entry_price   DOUBLE NOT NULL,
    shares        BIGINT NOT NULL,
    cost          DOUBLE NOT NULL,
    conditions_json VARCHAR NOT NULL DEFAULT '[]',
    last_price    DOUBLE NOT NULL DEFAULT 0,
    holding_days  BIGINT NOT NULL DEFAULT 0,
    updated_at    VARCHAR
);

CREATE SEQUENCE IF NOT EXISTS trade_log_id_seq;
CREATE TABLE IF NOT EXISTS trade_log (
    id            BIGINT PRIMARY KEY DEFAULT nextval('trade_log_id_seq'),
    run_id        BIGINT NOT NULL,
    date          VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    side          VARCHAR NOT NULL,
    trigger       VARCHAR NOT NULL,
    price         DOUBLE NOT NULL,
    shares        BIGINT NOT NULL,
    turnover      DOUBLE NOT NULL,
    commission    DOUBLE NOT NULL,
    stamp_tax     DOUBLE NOT NULL DEFAULT 0,
    transfer_fee  DOUBLE NOT NULL DEFAULT 0,
    slippage_amount DOUBLE NOT NULL DEFAULT 0,
    net_amount    DOUBLE NOT NULL DEFAULT 0,
    reason        VARCHAR NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_trade_log_date ON trade_log(date);
CREATE INDEX IF NOT EXISTS idx_trade_log_symbol ON trade_log(symbol);
CREATE INDEX IF NOT EXISTS idx_trade_log_run ON trade_log(run_id);

CREATE TABLE IF NOT EXISTS debug_snapshots (
    run_id        BIGINT NOT NULL,
    date          VARCHAR NOT NULL,
    snapshot_json VARCHAR NOT NULL,
    PRIMARY KEY (run_id, date)
);

CREATE TABLE IF NOT EXISTS ml_predictions (
    run_id        BIGINT NOT NULL,
    date          VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    model         VARCHAR NOT NULL,
    score         DOUBLE NOT NULL,
    PRIMARY KEY (run_id, date, symbol, model)
);
CREATE INDEX IF NOT EXISTS idx_ml_predictions_run ON ml_predictions(run_id);
"""

def connect_result_db(path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    """打开结果库连接；read_only 供纯读取入口（报告/回放/账本读取）。"""
    if read_only and path == ":memory:":
        raise ValueError(
            "内存结果库不支持 read_only（DuckDB 内存库无跨连接共享语义）"
        )
    assert_duckdb_file(path, "结果库")
    return duckdb.connect(path, read_only=read_only)


@contextmanager
def transaction(conn: duckdb.DuckDBPyConnection):
    """显式事务：成功 commit，异常 rollback。

    DuckDB 的 `with conn:` 语义是关闭连接（不是事务），事务必须显式管理。
    """
    conn.begin()
    try:
        yield conn
    except BaseException:
        conn.rollback()
        raise
    else:
        conn.commit()


def init_backtest_db(path: str) -> duckdb.DuckDBPyConnection:
    """初始化多 run 回测库（runs/account_daily/trade_log 按 run_id 累积）。

    同一 path 重复使用时，历史 run 保留，本次写入挂在新 run_id 下；
    holdings 是瞬态快照表，每次 run 开始清空。
    """
    assert_duckdb_file(path, "结果库")
    conn = duckdb.connect(path)
    conn.execute(SCHEMA_SQL)
    conn.execute("DELETE FROM holdings")
    return conn


def write_run(conn: duckdb.DuckDBPyConnection, run_id: int | None = None,
              **kwargs) -> int:
    """写入 runs 行并返回 run_id。

    run_id=None 时由 sequence 分配（多 run 结果库）；显式 run_id 供固定
    run_id 的调用方（实盘账本 LIVE_RUN_ID=1）使用——sequence 消耗不回滚，
    崩溃/回滚后若仍走 sequence，衍生表 run_id 会与 runs 行错位。
    """
    cols = ("created_at", "strategy", "start_date", "end_date",
            "initial_capital", "config_json", "status")
    values = [kwargs[c] for c in cols]
    if run_id is None:
        row = conn.execute(
            "INSERT INTO runs (" + ", ".join(cols) + ")"
            " VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING run_id",
            values,
        ).fetchone()
    else:
        row = conn.execute(
            "INSERT INTO runs (run_id, " + ", ".join(cols) + ")"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?) RETURNING run_id",
            [run_id, *values],
        ).fetchone()
    return int(row[0])


def write_daily(conn: duckdb.DuckDBPyConnection, run_id: int, date: str, cash: float,
                total_value: float, daily_pnl: float, cumulative_pnl: float,
                initial_capital: float, n_holdings: int = 0):
    # 显式列名（HYG-06）：schema 增列时位置 VALUES 会错位
    conn.execute(
        "INSERT OR REPLACE INTO account_daily"
        " (run_id, date, cash, total_value, daily_pnl, cumulative_pnl,"
        " initial_capital, n_holdings)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, date, cash, total_value, daily_pnl, cumulative_pnl,
         initial_capital, n_holdings),
    )


def write_holdings(conn: duckdb.DuckDBPyConnection, account):
    conn.execute("DELETE FROM holdings")
    # 循环外取一次时间戳（HYG-06）：同一快照所有行共享 updated_at
    updated_at = datetime.datetime.now().isoformat()
    rows = [
        (
            symbol,
            holding.entry_date,
            holding.entry_price,
            holding.shares,
            holding.cost,
            json.dumps(holding.conditions, default=str),
            holding.last_price,
            holding.holding_days,
            updated_at,
        )
        for symbol, holding in account.holdings.items()
    ]
    if rows:
        conn.executemany(
            "INSERT INTO holdings"
            " (symbol, entry_date, entry_price, shares, cost, conditions_json,"
            " last_price, holding_days, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )


_INSERT_TRADE_SQL = (
    "INSERT INTO trade_log (run_id, date, symbol, side, trigger, price,"
    " shares, turnover, commission, stamp_tax, transfer_fee,"
    " slippage_amount, net_amount, reason)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)


def trade_row(run_id: int, trade) -> tuple:
    return (
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
    )


def write_trade(conn: duckdb.DuckDBPyConnection, run_id: int, trade):
    conn.execute(_INSERT_TRADE_SQL, trade_row(run_id, trade))


def write_trades(conn: duckdb.DuckDBPyConnection, run_id: int, trades) -> None:
    """批量写入当日成交（executemany；调用方负责事务边界）。"""
    rows = [trade_row(run_id, t) for t in trades]
    if rows:
        conn.executemany(_INSERT_TRADE_SQL, rows)


def update_run_status(conn: duckdb.DuckDBPyConnection, run_id: int, status: str):
    conn.execute("UPDATE runs SET status = ? WHERE run_id = ?", (status, run_id))


def _json_default(obj):
    # numpy 标量/pd.Timestamp 等经 item()/str() 降级为 JSON 可序列化值
    if hasattr(obj, "item"):
        return obj.item()
    return str(obj)


def write_run_stats(conn: duckdb.DuckDBPyConnection, run_id: int, stats: dict):
    """把 statistics dict 以 JSON 形式挂到 runs.stats_json，供多 run 对比。"""
    conn.execute(
        "UPDATE runs SET stats_json = ? WHERE run_id = ?",
        (json.dumps(stats, default=_json_default), run_id),
    )


def read_runs(conn: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """runs 全表（按 run_id 升序），多 run 对比的入口。"""
    return conn.execute("SELECT * FROM runs ORDER BY run_id").df()


def read_run_data(conn: duckdb.DuckDBPyConnection, run_id: int):
    """读取单个 run 的 (account_daily, trade_log, stats_dict|None)。

    stats_json 为 NULL 时 stats_dict 返回 None，调用方自行重算。
    """
    account_daily = conn.execute(
        "SELECT * FROM account_daily WHERE run_id = ? ORDER BY date", [run_id]
    ).df()
    trade_log = conn.execute(
        "SELECT * FROM trade_log WHERE run_id = ? ORDER BY date, id", [run_id]
    ).df()
    row = conn.execute(
        "SELECT stats_json FROM runs WHERE run_id = ?", [run_id]
    ).fetchone()
    stats = json.loads(row[0]) if row and row[0] else None
    return account_daily, trade_log, stats


def write_ml_predictions(
    conn: duckdb.DuckDBPyConnection, run_id: int, date: str, rows: list[tuple]
) -> None:
    """批量写入 ML 分数：rows = [(model, symbol, score), ...]。

    score 为 None/NaN（特征缺失过半 → 无分数）的行跳过——schema 的 score 列
    NOT NULL，None 会让整批写入约束错误崩溃。
    """
    valid = [
        (run_id, date, sym, model, score)
        for model, sym, score in rows
        if score is not None and not (isinstance(score, float) and math.isnan(score))
    ]
    if not valid:
        return
    conn.executemany(
        "INSERT OR REPLACE INTO ml_predictions (run_id, date, symbol, model, score) "
        "VALUES (?, ?, ?, ?, ?)",
        valid,
    )


def write_debug_snapshot(conn: duckdb.DuckDBPyConnection, run_id: int, date: str,
                         snapshot: dict) -> None:
    """写入每日调试快照（debug 模式）。"""
    conn.execute(
        "INSERT OR REPLACE INTO debug_snapshots (run_id, date, snapshot_json) "
        "VALUES (?, ?, ?)",
        (run_id, date, json.dumps(snapshot, ensure_ascii=False, default=str)),
    )
