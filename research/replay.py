"""交易回放 — 从结果库加载 debug_snapshots，输出决策上下文。

scripts/replay.py 薄壳的全部逻辑，抽到 research 层可 import 复用
（整改指导 DUP-08：唯一未薄壳化的脚本）。
"""

import json
import sys

import duckdb

from btcore import database
from research.cli_common import latest_run_id

# 因子展示时排除的行情/派生列（与决策无关，避免刷屏）
_FACTOR_EXCLUDE = {
    "open", "high", "low", "close", "vol", "adj_factor",
    "pre_close", "up_limit", "down_limit",
    "open_hfq", "high_hfq", "low_hfq", "close_hfq",
    "pct_chg", "amount", "trade_date",
}


def resolve_run_id(conn: duckdb.DuckDBPyConnection, run_id: int | None) -> int:
    """run_id 解析：显式值优先（校验存在），缺省取最新 run；异常 → ValueError。

    显式 run_id 不存在时报「run N 不存在」（而非让后续快照查询静默返回空）；
    旧 SQLite 结果库在 connect_result_db 即被拒（不保留兼容），因此不再有
    "无 runs 表回退 run 1"路径。
    """
    if run_id is not None:
        try:
            row = conn.execute(
                "SELECT 1 FROM runs WHERE run_id = ?", [run_id]
            ).fetchone()
        except duckdb.CatalogException:
            row = None
        if row is None:
            raise ValueError(f"run {run_id} 不存在")
        return run_id
    try:
        rid = latest_run_id(conn)
    except duckdb.CatalogException:
        rid = None  # 无 runs 表（非标准结果库）
    if rid is None:
        raise ValueError("结果库中无 run 记录")
    return rid


def load_snapshots(conn, run_id: int, date: str | None = None) -> list[tuple[str, dict]]:
    """按 run_id（+date 过滤可选）读取快照，返回 [(date, snapshot_dict)] 升序。"""
    query = "SELECT date, snapshot_json FROM debug_snapshots WHERE run_id = ?"
    params = [run_id]
    if date:
        query += " AND date = ?"
        params.append(date)
    query += " ORDER BY date"
    return [(d, json.loads(s)) for d, s in conn.execute(query, params).fetchall()]


def day_symbols(snap: dict) -> list[str]:
    """快照当日涉及的 symbol（bars_subset ∪ holdings_detail，排序去重）。"""
    bars = snap.get("bars_subset", {})
    holdings = snap.get("holdings_detail", {})
    return sorted(set(bars) | set(holdings))


def format_day(snap: dict, symbol: str | None = None) -> list[str]:
    """单日快照 → 文本行（symbol 过滤；无匹配返回空列表）。"""
    bars = snap.get("bars_subset", {})
    holdings = snap.get("holdings_detail", {})
    if symbol and symbol not in (set(bars) | set(holdings)):
        return []
    lines = [f"=== {snap['date']} ==="]
    lines.append(f"账户: cash={snap['account']['cash']:.0f} "
                 f"total={snap['account']['total_value']:.0f} "
                 f"holdings={snap['account']['n_holdings']}")
    pending = snap.get("pending", {})
    if pending.get("buy"):
        lines.append(f"  BUY: {pending['buy']}")
    if pending.get("sell"):
        lines.append(f"  SELL: {pending['sell']}")
    if pending.get("buy_conditions"):
        lines.append(f"  BUY_COND: {[c['symbol'] for c in pending['buy_conditions']]}")
    for sym, h in holdings.items():
        bar = bars.get(sym, {})
        factor_keys = [k for k in bar if k not in _FACTOR_EXCLUDE]
        factor_str = ", ".join(f"{k}={bar.get(k)}" for k in factor_keys[:5])
        lines.append(f"  {sym}: shares={h['shares']} entry={h['entry_price']} "
                     f"days={h['holding_days']} close={bar.get('close')} {factor_str}")
    return lines


def run_replay(db_path: str, run_id: int | None = None, *, symbol: str | None = None,
               date: str | None = None, list_symbols: bool = False) -> int:
    """完整回放输出到 stdout；0 = 成功，1 = 无 run / 无匹配快照 / 库不可读。

    与原 scripts/replay.py 的 CLI 输出逐字节一致。
    """
    try:
        conn = database.connect_result_db(db_path, read_only=True)
    except (duckdb.Error, ValueError) as e:
        print(f"无法打开结果库 {db_path}: {e}", file=sys.stderr)
        return 1
    try:
        try:
            rid = resolve_run_id(conn, run_id)
        except ValueError as e:
            print(e, file=sys.stderr)
            return 1
        snaps = load_snapshots(conn, rid, date=date)
        if not snaps:
            print("无匹配快照", file=sys.stderr)
            return 1
        matched = 0
        for d, snap in snaps:
            if list_symbols:
                print(f"[{d}] {', '.join(day_symbols(snap))}")
                matched += 1
                continue
            lines = format_day(snap, symbol=symbol)
            if not lines:
                continue
            matched += 1
            for line in lines:
                print(line)
            print()
        if matched == 0:
            print("无匹配快照", file=sys.stderr)
            return 1
    finally:
        conn.close()
    return 0
