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
    cash: 41233.55                       # 券商可用资金（非负，禁止 NaN/Inf）
    holdings: [{symbol: 600519.SH, shares: 100}]   # 券商实际持仓
    fills:                               # 今日实际成交（可为空；禁止 OPENING）
      - {symbol: 000001.SZ, side: SELL, price: 12.34, shares: 1000,
         commission: 2.47, stamp_tax: 6.17, transfer_fee: 0.0, reason: TREND_BREAK}
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import yaml

from btcore import database
from btcore.strategy_loader import load_strategy
from research import cli_common
from research.live import (
    LedgerStore,
    build_op_sheet,
    has_market_data,
    reconcile,
    require_finite,
    run_signal,
    validate_date_format,
    validate_fill_dates,
    validate_open_day,
)

CASH_SYNC_EPS = 0.01          # 现金差额低于此值不记 ADJUST（分位噪声）
CASH_SYNC_WARN = 100.0        # 现金自动调整超过此值输出 warning


def _print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _error_json(stage: str, message: str, **extra) -> None:
    payload = {"ok": False, "stage": stage, "message": message}
    payload.update(extra)
    _print_json(payload)


def _load_yaml(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _parse_statement(path: str) -> dict:
    """sync.yaml 解析 + 数值校验（LIVE-11）；错误经 cmd_sync 的 JSON 通道。"""
    try:
        stmt = _load_yaml(path)
    except OSError as exc:
        raise ValueError(f"sync.yaml 无法读取: {exc}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"sync.yaml 解析失败: {exc}") from None
    if not isinstance(stmt, dict):
        raise ValueError(f"sync.yaml 顶层必须是映射，实际 {type(stmt).__name__}")

    raw_date = stmt.get("date")
    date = str(raw_date) if raw_date is not None else ""
    try:
        validate_date_format(date)
    except ValueError as exc:
        raise ValueError(f"sync.yaml date 非法: {exc}") from None

    cash = require_finite(stmt.get("cash"), "cash")
    if cash < 0:
        raise ValueError(f"券商现金为负 {cash:.2f}：账本不支持融资负现金")

    raw_holdings = stmt.get("holdings") or []
    if not isinstance(raw_holdings, list):
        raise ValueError(
            f"holdings 必须是列表，实际 {type(raw_holdings).__name__}")
    holdings: dict[str, int] = {}
    for i, h in enumerate(raw_holdings, start=1):
        if not isinstance(h, dict) or "symbol" not in h or "shares" not in h:
            raise ValueError(f"holdings 第 {i} 条缺 symbol/shares: {h!r}")
        value = require_finite(h["shares"], f"holdings 第 {i} 条 shares")
        if not float(value).is_integer() or value <= 0:
            raise ValueError(
                f"holdings 第 {i} 条 shares 必须为正整数: {h['shares']!r}")
        holdings[str(h["symbol"])] = int(value)

    raw_fills = stmt.get("fills") or []
    if not isinstance(raw_fills, list):
        raise ValueError(f"fills 必须是列表，实际 {type(raw_fills).__name__}")
    for i, f in enumerate(raw_fills, start=1):
        if not isinstance(f, dict):
            raise ValueError(f"fills 第 {i} 条必须是映射: {f!r}")
    return {"date": date, "cash": cash, "holdings": holdings, "fills": raw_fills}


def _open_ledger(path: str) -> LedgerStore:
    """只打开已存在的账本（LIVE-09）：不存在的路径不静默建库。

    先用只读连接探测 ledger_meta/initial_capital，避免把回测结果库等
    非账本文件误当账本打开（LedgerStore 打开会写建表语句）。
    """
    if not os.path.isfile(path):
        raise ValueError(f"账本库不存在: {path}（先运行 init）")
    try:
        probe = database.connect_result_db(path, read_only=True)
    except Exception as exc:
        raise ValueError(f"{path} 不是可读的 DuckDB 账本库: {exc}") from None
    try:
        has_meta = probe.execute(
            "SELECT 1 FROM information_schema.tables"
            " WHERE table_name = 'ledger_meta'"
        ).fetchone()
        if has_meta is None:
            raise ValueError(f"{path} 不是账本库（缺 ledger_meta；先运行 init）")
        initialized = probe.execute(
            "SELECT 1 FROM ledger_meta WHERE key = 'initial_capital'"
        ).fetchone()
        if initialized is None:
            raise ValueError(f"{path} 不是已初始化账本（先运行 init）")
    finally:
        probe.close()
    store = LedgerStore(path)
    try:
        store.start_date  # 触发格式校验（INFRA-05 遗留脏数据 fail-fast）
    except ValueError:
        store.close()
        raise
    return store


def _normalize_positions(positions: list) -> list[tuple]:
    """建账持仓整体预校验 → [(entry_date, symbol, price, shares)]。

    任何条目非法在写库前报错（避免"已初始化但持仓残缺"的半账本）。
    """
    out = []
    for p in positions:
        try:
            symbol = str(p.get("symbol") or "")
            entry_date = str(p.get("entry_date") or "")
        except AttributeError:
            raise ValueError(f"positions 条目非法（非映射）: {p!r}") from None
        try:
            price = require_finite(p.get("entry_price"), "positions entry_price")
            shares_value = require_finite(p.get("shares"), "positions shares")
        except ValueError as exc:
            raise ValueError(f"positions 条目非法: {p!r}（{exc}）") from None
        if not symbol or len(entry_date) != 8 or not entry_date.isdigit():
            raise ValueError(f"positions 条目非法（symbol/日期）: {p!r}")
        if price <= 0 or shares_value <= 0:
            raise ValueError(f"positions 条目非法（价格/股数）: {p!r}")
        if not float(shares_value).is_integer():
            raise ValueError(f"positions 条目非法（股数必须为整数）: {p!r}")
        out.append((entry_date, symbol, price, int(shares_value)))
    return out


def cmd_init(args, provider=None) -> int:
    """建账。

    provider 由 main() 注入用于开市日校验（LIVE-06）；直接以编程方式调用
    且未提供 provider 时跳过开市日校验（仍做格式/数值校验），供单测使用。
    """
    try:
        date = validate_date_format(args.date)
        cash = require_finite(args.cash, "--cash")
        positions = []
        if args.positions:
            positions = _load_yaml(args.positions).get("positions") or []
        normalized = _normalize_positions(positions)
        if provider is not None:
            validate_open_day(date, provider)
    except (AttributeError, OSError, ValueError, yaml.YAMLError) as exc:
        _error_json("bad_args", str(exc))
        return 2

    store = None
    try:
        try:
            store = LedgerStore(args.db)
        except ValueError as exc:  # schema_version 未知等
            _error_json("bad_args", str(exc))
            return 2
        cost = sum(shares * price for _, _, price, shares in normalized)
        initial_capital = cash + cost
        now = datetime.datetime.now().isoformat()
        # 元数据 + OPENING 持仓同一事务：任一失败整体回滚，不留半初始化账本
        store.conn.begin()
        try:
            store.init_account(date, cash, initial_capital)
            for entry_date, symbol, price, shares in normalized:
                store.append_fill(
                    entry_date, symbol, "OPENING",
                    price=price, shares=shares,
                    reason="OPENING", created_at=now,
                )
            store.conn.commit()
        except ValueError as exc:
            store.conn.rollback()
            _error_json("bad_args", str(exc))
            return 2
        except BaseException:
            store.conn.rollback()
            raise
        _print_json({
            "ok": True, "db": args.db, "start_date": date,
            "initial_cash": cash, "initial_capital": initial_capital,
            "opening_positions": len(normalized),
        })
    finally:
        if store is not None:
            store.close()
    return 0


def cmd_sync(args) -> int:
    try:
        stmt = _parse_statement(args.file)
    except ValueError as exc:
        _error_json("parse_error", str(exc))
        return 2
    statement_date = stmt["date"]
    actual_cash = stmt["cash"]
    actual_holdings = stmt["holdings"]
    fills = stmt["fills"]

    store = None
    provider = None
    try:
        try:
            store = _open_ledger(args.db)
        except ValueError as exc:
            _error_json("bad_db", str(exc))
            return 2
        provider = cli_common.make_provider()
        # 派生回放只遍历开市日：非开市 statement date 的 fill/ADJUST 永不生效
        # （append-only 死行）且每次 sync 按差额重复追加 → 归一到 <=date 最近开市日
        date = statement_date
        if date not in provider.get_calendar(date, date):
            settle = provider.prev_trading_day(date)
            if settle is None:
                _error_json(
                    "bad_statement",
                    f"date {date} 之前找不到交易日（行情数据未更新？）",
                )
                return 2
            date = settle
        if date < store.start_date:
            _error_json(
                "bad_statement",
                f"date {date} 早于账本起始日 {store.start_date}",
            )
            return 2
        # LIVE-07：statement date 晚于行情数据覆盖范围 → fail-fast（与 signal
        # 对 signal_date 要求有 bars 同口径）；trade_cal 含未来日期，光看日历不够
        if not has_market_data(provider, date):
            _error_json(
                "no_market_data",
                f"{date} 无行情数据（statement date {statement_date}）"
                "——请先更新行情数据库",
            )
            return 1
        # LIVE-01：fill 显式日期（含 statement 归一化缺省日）必须落在
        # [账本起始日, 对账日] 的交易日历内，否则整体拒绝、不落库
        calendar = provider.get_calendar(store.start_date, date)
        problems = validate_fill_dates(fills, calendar, default_date=date)
        if problems:
            _error_json(
                "invalid_fill_date",
                "fill 日期不在交易日历内（账本起始日 ~ 对账日），已整体拒绝",
                problems=problems,
            )
            return 1

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
            _error_json(
                "data_error",
                f"账本数据错误，已回滚：{exc}",
            )
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
            "trade_date": date,
            "statement_date": statement_date,
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
    """回放账本 → 明日操作单；业务错误打可读消息而非裸 traceback（LIVE-12）。"""
    try:
        end = (validate_date_format(args.date) if args.date
               else datetime.datetime.now().strftime("%Y%m%d"))
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if not Path(args.yaml).is_file():
        print(f"策略 YAML 不存在: {args.yaml}", file=sys.stderr)
        return 2
    out = args.out or str(Path(args.db).parent / f"opsheet_{end}.json")
    out_dir = Path(out).parent
    if not out_dir.is_dir():
        print(f"--out 目录不存在: {out_dir}", file=sys.stderr)
        return 2

    store = None
    provider = None
    try:
        try:
            store = _open_ledger(args.db)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        if end < store.start_date:
            print(f"date {end} 早于账本起始日 {store.start_date}", file=sys.stderr)
            return 2
        try:
            strategy = load_strategy(args.yaml)
        except (KeyError, OSError, ValueError) as exc:
            print(f"策略 YAML 加载失败: {exc}", file=sys.stderr)
            return 2
        provider = cli_common.make_provider()
        try:
            engine, calendar = run_signal(strategy, provider, store, end)
        except ValueError as exc:
            print(f"signal 失败: {exc}", file=sys.stderr)
            return 1
        sheet = build_op_sheet(engine, provider, calendar[-1])
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
    try:
        store = _open_ledger(args.db)
    except ValueError as exc:
        _error_json("bad_db", str(exc))
        return 2
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
    if args.cmd == "init":
        # init 需要 provider 校验开市日（INFRA-05/LIVE-06）；构造失败即 fail-fast。
        # 直接调用 cmd_init 的单测可自行注入 provider 以保持无真实库依赖。
        try:
            provider = cli_common.make_provider()
        except Exception as exc:
            _error_json("provider_error",
                        f"行情库不可用，无法校验开市日: {exc}")
            return 2
        try:
            return cmd_init(args, provider)
        finally:
            provider.backend.close()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
