"""实盘账本 CLI — 账本维护与每日信号。

用法:
    # 建仓（开户）：现金 + 已有持仓（entry_date/entry_price 用于 holding_days
    # 与 trailing 锚点重建；positions 文件可省，空仓开局）
    python scripts/live.py init live/main.duckdb --date 20260731 --cash 40000 \
        [--positions positions.yaml]

    # 每日对账同步：全量账户信息一次性给到位（现金+持仓+今日成交）
    python scripts/live.py sync live/main.duckdb sync.yaml

    # 每日信号：回放账本 → 明日操作单（开盘手动单 + 券商条件单 + 提示）
    python scripts/live.py signal live/main.duckdb strategies/selected/xxx/config.yaml \
        [--date 20260803] [--out opsheet.json]

    # 当前状态（衍生账户视图 + 最近成交）
    python scripts/live.py status live/main.duckdb

账本与策略完全解耦：ledger_fills 是唯一手工数据源（append-only），
runs/trade_log/account_daily/holdings 为每次 signal 重写的衍生表，
与回测结果库同 schema（report.py/cross_validate.py 可直接消费）。
sync.yaml 格式:
    date: 20260803
    cash: 41233.55                       # 券商可用资金
    holdings: [{symbol: 600519.SH, shares: 100}]   # 券商实际持仓
    fills:                               # 今日实际成交（可为空）
      - {symbol: 000001.SZ, side: SELL, price: 12.34, shares: 1000,
         commission: 2.47, stamp_tax: 6.17, transfer_fee: 0.0, reason: TREND_BREAK}
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import yaml

from btcore.strategy_loader import load_strategy
from research import cli_common
from research.live import LedgerStore, build_op_sheet, reconcile, run_signal

CASH_SYNC_EPS = 0.01          # 现金差额低于此值不记 ADJUST（分位噪声）
CASH_SYNC_WARN = 100.0        # 现金自动调整超过此值输出 warning


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _normalize_positions(positions: list) -> list[tuple]:
    """建账持仓整体预校验 → [(entry_date, symbol, price, shares)]。

    任何条目非法在写库前报错（避免"已初始化但持仓残缺"的半账本）。
    """
    out = []
    for p in positions:
        try:
            symbol = str(p.get("symbol") or "")
            entry_date = str(p.get("entry_date") or "")
            price = float(p.get("entry_price"))
            shares = int(p.get("shares"))
        except (AttributeError, KeyError, TypeError, ValueError):
            raise ValueError(f"positions 条目非法（缺字段/类型错误）: {p!r}") from None
        if not symbol or len(entry_date) != 8 or price <= 0 or shares <= 0:
            raise ValueError(f"positions 条目非法（symbol/日期/价格/股数）: {p!r}")
        out.append((entry_date, symbol, price, shares))
    return out


def cmd_init(args) -> int:
    store = LedgerStore(args.db)
    try:
        positions = []
        if args.positions:
            positions = _load_yaml(args.positions).get("positions") or []
        try:
            normalized = _normalize_positions(positions)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        cost = sum(shares * price for _, _, price, shares in normalized)
        initial_capital = args.cash + cost
        now = datetime.datetime.now().isoformat()
        # 元数据 + OPENING 持仓同一事务：任一失败整体回滚，不留半初始化账本
        store.conn.begin()
        try:
            store.init_account(args.date, args.cash, initial_capital)
            for entry_date, symbol, price, shares in normalized:
                store.append_fill(
                    entry_date, symbol, "OPENING",
                    price=price, shares=shares,
                    reason="OPENING", created_at=now,
                )
            store.conn.commit()
        except BaseException:
            store.conn.rollback()
            raise
        _print_json({
            "ok": True, "db": args.db, "start_date": args.date,
            "initial_cash": args.cash, "initial_capital": initial_capital,
            "opening_positions": len(normalized),
        })
    finally:
        store.close()
    return 0


def cmd_sync(args) -> int:
    stmt = _load_yaml(args.file)
    date = str(stmt.get("date") or "")
    if len(date) != 8:
        print("sync.yaml 缺 date (YYYYMMDD)", file=sys.stderr)
        return 2
    actual_holdings = {
        h["symbol"]: int(h["shares"]) for h in (stmt.get("holdings") or [])
    }
    actual_cash = float(stmt.get("cash"))
    fills = stmt.get("fills") or []

    store = None
    provider = None
    try:
        store = LedgerStore(args.db)
        provider = cli_common.make_provider()
        if date < store.start_date:
            print(f"date {date} 早于账本起始日 {store.start_date}", file=sys.stderr)
            return 2
        now = datetime.datetime.now().isoformat()
        # DuckDB 默认自动提交：回滚语义要求显式开启事务（含后续 ADJUST）
        store.conn.begin()
        try:
            appended, skipped = store.append_fills_idempotent(
                fills, now, default_date=date
            )
            report = reconcile(store, provider, date, actual_cash, actual_holdings)
        except Exception as exc:  # 数据一致性错误（重复/缺买入等）→ 回滚 + JSON
            store.conn.rollback()
            _print_json({
                "ok": False, "stage": "data_error",
                "message": f"账本数据错误，已回滚：{exc}",
            })
            return 1

        if not report.ok:
            store.conn.rollback()
            _print_json({
                "ok": False, "stage": "reconcile",
                "message": "持仓不一致，已回滚本次 fills；请核对是否漏录/错录成交",
                "holding_diffs": {
                    s: {"derived": d, "actual": a}
                    for s, (d, a) in report.holding_diffs.items()
                },
                "derived_holdings": report.derived_holdings,
                "actual_holdings": report.actual_holdings,
            })
            return 1

        cash_adjust = 0.0
        if abs(report.cash_delta) > CASH_SYNC_EPS:
            cash_adjust = report.cash_delta
            store.append_fill(date, "", "ADJUST", price=cash_adjust,
                              reason="sync_auto", created_at=now)
        store.conn.commit()
        _print_json({
            "ok": True, "date": date,
            "fills_applied": appended,
            "fills_skipped_dup": skipped,
            "cash_derived": round(report.cash_derived, 2),
            "cash_actual": round(report.cash_actual, 2),
            "cash_adjust": round(cash_adjust, 2),
            "warning": (
                f"现金自动调整 {cash_adjust:+.2f} 超过 {CASH_SYNC_WARN:.0f} 元，"
                "请确认是否为出入金/漏录费用" if abs(cash_adjust) > CASH_SYNC_WARN else None
            ),
        })
    finally:
        if provider is not None:
            provider.backend.close()
        if store is not None:
            store.close()
    return 0


def cmd_signal(args) -> int:
    store = None
    provider = None
    try:
        store = LedgerStore(args.db)
        provider = cli_common.make_provider()
        end = args.date or datetime.datetime.now().strftime("%Y%m%d")
        if end < store.start_date:
            print(f"date {end} 早于账本起始日 {store.start_date}", file=sys.stderr)
            return 2
        strategy = load_strategy(args.yaml)
        engine, calendar = run_signal(strategy, provider, store, end)
        sheet = build_op_sheet(engine, provider, calendar[-1])
        out = args.out
        if out is None:
            out = str(Path(args.db).parent / f"opsheet_{calendar[-1]}.json")
        Path(out).write_text(
            json.dumps(sheet, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        sheet["_opsheet_file"] = out
        _print_json(sheet)
    finally:
        if provider is not None:
            provider.backend.close()
        if store is not None:
            store.close()
    return 0


def cmd_status(args) -> int:
    store = LedgerStore(args.db)
    try:
        daily = store.conn.execute(
            "SELECT date, cash, total_value, n_holdings FROM account_daily"
            " WHERE run_id = 1 ORDER BY date DESC LIMIT 1"
        ).fetchone()
        holdings = store.conn.execute(
            "SELECT symbol, shares, entry_date, entry_price, last_price,"
            " holding_days FROM ledger_holdings ORDER BY symbol"
        ).fetchall()
        recent = store.conn.execute(
            "SELECT date, symbol, side, price, shares, reason FROM ledger_fills"
            " ORDER BY id DESC LIMIT 10"
        ).fetchall()
        _print_json({
            "db": args.db,
            "start_date": store.get_meta("start_date"),
            "initial_capital": store.get_meta("initial_capital"),
            "last_day": (
                dict(zip(["date", "cash", "total_value", "n_holdings"], daily))
                if daily else None
            ),
            "holdings": [
                dict(zip(["symbol", "shares", "entry_date", "entry_price",
                          "last_price", "holding_days"], h))
                for h in holdings
            ],
            "recent_fills": [
                dict(zip(["date", "symbol", "side", "price", "shares", "reason"], r))
                for r in reversed(recent)
            ],
        })
    finally:
        store.close()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="实盘账本：sync 对账 + signal 明日操作单")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="建账（现金 + 可选已有持仓）")
    p.add_argument("db", help="账本库路径（如 live/main.duckdb）")
    p.add_argument("--date", required=True, help="建账日 YYYYMMDD")
    p.add_argument("--cash", type=float, required=True, help="当前可用资金")
    p.add_argument("--positions", default=None, help="已有持仓 YAML（可省）")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("sync", help="每日对账同步（全量账户信息）")
    p.add_argument("db")
    p.add_argument("file", help="sync.yaml（date/cash/holdings/fills）")
    p.set_defaults(fn=cmd_sync)

    p = sub.add_parser("signal", help="回放账本 → 明日操作单")
    p.add_argument("db")
    p.add_argument("yaml", help="策略 YAML（可随意切换）")
    p.add_argument("--date", default=None, help="信号日 YYYYMMDD（缺省今天）")
    p.add_argument("--out", default=None, help="操作单 JSON 输出路径")
    p.set_defaults(fn=cmd_signal)

    p = sub.add_parser("status", help="账本当前状态")
    p.add_argument("db")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
