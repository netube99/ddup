"""实盘 V2 CLI — 账户事实 / 策略会话 / 执行计划（docs/live_guide.md）。

用法（每日闭环，详见 docs/live_guide.md#workflow）:
    python scripts/live.py init live/main.duckdb --account-id main
    python scripts/live.py import live/main.duckdb statement.yaml
    python scripts/live.py confirm live/main.duckdb --snapshot 3 [--takeover --reason R]
    python scripts/live.py activate live/main.duckdb --date 20260803 \
        --yaml strategies/selected/xxx/config.yaml [--anchors anchors.yaml]
    python scripts/live.py preview live/main.duckdb [--date 20260803] [--json]
    python scripts/live.py accept live/main.duckdb [--date 20260803]
    python scripts/live.py submit live/main.duckdb --order <client_order_id>
    python scripts/live.py fill live/main.duckdb --order <id> --date D \
        --qty N --price P [--commission C] [--tax T] [--transfer F] [--other O]
    python scripts/live.py status live/main.duckdb

约定：成功输出 JSON（含 ok:true）；业务/输入错误输出
{ok:false, code, message} 并以非零码退出（2=输入，1=业务/状态）。
preview/status 只读；其余写命令幂等或整体回滚，不会留半笔状态。
--fixtures-dir 仅供离线演练（tests fixtures 行情），正常实盘勿用。
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

import yaml

from btcore.provider import DataProvider
from research import cli_common
from research.live import (
    _diff_dict,
    import_fills_file,
    import_statement,
    preview_payload,
    render_preview_table,
    render_status_table,
    status_payload,
)
from research.live_account import AccountLedger, LiveAccountError
from research.live_plan import PlanStore
from research.live_strategy import StrategySession


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _error(code: str, message: str, **extra) -> None:
    payload = {"ok": False, "code": code, "message": message}
    payload.update(extra)
    _print_json(payload)


def _load_yaml(path: str):
    if not Path(path).is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    with open(path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise ValueError(f"YAML 顶层必须是映射: {path}")
    return payload


def _make_provider(fixtures_dir: str | None = None) -> DataProvider:
    if fixtures_dir:
        from tests.conftest import MockDataBackend

        backend = MockDataBackend(fixtures_dir)
        provider = DataProvider(backend)
        provider._live_cli_closer = getattr(backend, "close", None)
        return provider
    return cli_common.make_provider()


def _close_provider(provider: DataProvider) -> None:
    """fixtures 后端可能无 close（无资源可释放）；真实后端照常关闭。"""
    closer = getattr(provider, "_live_cli_closer", None)
    if closer is None:
        closer = getattr(provider.backend, "close", None)
    if callable(closer):
        closer()


def _open_ledger(db: str, *, read_only: bool = False) -> AccountLedger:
    return AccountLedger.open(db, read_only=read_only)


def _open_session(db: str, provider: DataProvider, *, strategy=None):
    ledger = _open_ledger(db)
    try:
        session = StrategySession.open(ledger, provider, strategy=strategy)
    except LiveAccountError:
        ledger.close()
        raise
    return ledger, session


def _load_yaml_strategy(yaml_path: str):
    from btcore.strategy_loader import load_strategy

    return load_strategy(yaml_path)


def _today() -> str:
    return datetime.datetime.now().strftime("%Y%m%d")


# ── 子命令 ──


def cmd_init(args) -> int:
    ledger = AccountLedger.create(
        args.db, account_id=args.account_id, broker=args.broker,
        note=args.note)
    try:
        _print_json({"ok": True, "db": args.db,
                     "account_id": ledger.account_id,
                     "schema_version": ledger.status().schema_version})
    finally:
        ledger.close()
    return 0


def cmd_import(args) -> int:
    statement = _load_yaml(args.file)
    ledger = _open_ledger(args.db)
    try:
        result = import_statement(ledger, statement, source=args.source)
        snapshot = result["snapshot"]
        payload = {
            "ok": True,
            "snapshot": {"snapshot_id": snapshot.snapshot.snapshot_id,
                         "as_of": snapshot.snapshot.as_of,
                         "action": snapshot.action},
        }
        if result["fills"] is not None:
            payload["fills"] = {"appended": result["fills"].appended,
                                "skipped": result["fills"].skipped}
        payload["cash_flows"] = len(result["cash_flows"])
        payload["share_adjustments"] = len(result["share_adjustments"])
        if snapshot.action == "stored_candidate" and snapshot.diff is not None:
            payload["pending_diffs"] = _diff_dict(snapshot.diff)
            payload["next"] = ("补录解释差异的事实（cashflow/adjust-shares）后"
                               " confirm --snapshot 重算；或 --takeover 接管")
        _print_json(payload)
    finally:
        ledger.close()
    return 0


def cmd_import_fills(args) -> int:
    ledger = _open_ledger(args.db)
    try:
        result = import_fills_file(
            ledger, args.file, source=args.source,
            default_date=args.date, batch_id=args.batch_id)
        _print_json({"ok": True, "appended": result.appended,
                    "skipped": result.skipped,
                    "action": result.action})
    finally:
        ledger.close()
    return 0


def cmd_confirm(args) -> int:
    ledger = _open_ledger(args.db)
    try:
        if args.takeover:
            if not args.reason:
                _error("validation", "接管必须提供 --reason（审计可追溯）")
                return 2
            snapshot = ledger.takeover_candidate(args.snapshot,
                                                 reason=args.reason)
            _print_json({"ok": True, "action": "takeover_confirmed",
                         "snapshot_id": snapshot.snapshot_id,
                         "as_of": snapshot.as_of})
            return 0
        result = ledger.reconcile_candidate(args.snapshot)
        payload = {"ok": True, "action": result.action,
                   "snapshot_id": result.snapshot.snapshot_id,
                   "confirmed": result.confirmed}
        if result.diff is not None and not result.confirmed:
            payload["remaining_diffs"] = _diff_dict(result.diff)
        _print_json(payload)
    finally:
        ledger.close()
    return 0


def cmd_cashflow(args) -> int:
    ledger = _open_ledger(args.db)
    try:
        flow = ledger.record_cash_flow(
            date=args.date or _today(), category=args.category,
            amount=args.amount, symbol=args.symbol or "",
            source=args.source,
            event_id=args.event_id or
            f"cli:{args.category}:{args.date or _today()}:{args.amount}",
            note=args.note)
        _print_json({"ok": True, "flow_id": flow.flow_id,
                     "date": flow.date, "category": flow.category,
                     "amount": flow.amount})
    finally:
        ledger.close()
    return 0


def cmd_adjust_shares(args) -> int:
    ledger = _open_ledger(args.db)
    try:
        record = ledger.record_share_adjustment(
            date=args.date or _today(), symbol=args.symbol,
            delta_shares=args.delta, category=args.category,
            reason=args.reason, source=args.source,
            event_id=args.event_id or
            f"cli:{args.category}:{args.symbol}:{args.date or _today()}:{args.delta}")
        _print_json({"ok": True, "adjustment_id": record.adjustment_id,
                     "date": record.date, "symbol": record.symbol,
                     "delta_shares": record.delta_shares})
    finally:
        ledger.close()
    return 0


def cmd_policy(args) -> int:
    ledger = _open_ledger(args.db)
    try:
        if args.clear:
            ledger.set_holding_policy(args.symbol, "observe",
                                      reason=args.reason or "clear policy")
        else:
            kwargs = {"reason": args.reason or ""}
            if args.policy == "managed":
                if not (args.anchor_date and args.anchor_price):
                    _error("validation",
                           "managed 政策需要 --anchor-date 与 --anchor-price"
                           "（不编造入场锚点）")
                    return 2
                kwargs.update(anchor_date=args.anchor_date,
                              anchor_price=args.anchor_price)
            if args.expire_date:
                kwargs["expire_date"] = args.expire_date
            ledger.set_holding_policy(args.symbol, args.policy, **kwargs)
        policy = ledger.holding_policy(args.symbol)
        _print_json({"ok": True, "symbol": args.symbol,
                     "policy": ledger.effective_policy(args.symbol),
                     "declared": policy.policy if policy else None})
    finally:
        ledger.close()
    return 0


def cmd_activate(args) -> int:
    strategy = _load_yaml_strategy(args.yaml)
    provider = _make_provider(args.fixtures_dir)
    ledger, session = _open_session(args.db, provider, strategy=strategy)
    try:
        anchors = _load_yaml(args.anchors).get("anchors") \
            if args.anchors else None
        record = session.activate(
            effective_date=args.date, yaml_path=args.yaml,
            anchors=anchors, manage_anchored=not args.no_manage,
            note=args.note)
        _print_json({
            "ok": True, "activation_id": record.activation_id,
            "effective_date": record.effective_date,
            "strategy": record.identity.name,
            "config_hash": record.identity.config_hash[:12],
            "code_hash": record.identity.code_hash[:12],
            "anchors": sorted(record.anchors),
        })
    finally:
        _close_provider(provider)
        ledger.close()
    return 0


def _compute_view(args, session, *, trial_yaml: str | None = None):
    if trial_yaml:
        return session.compute_trial(
            _load_yaml_strategy(trial_yaml), signal_date=args.date)
    return session.compute_preview(signal_date=args.date)


def cmd_preview(args) -> int:
    provider = _make_provider(args.fixtures_dir)
    ledger, session = _open_session(args.db, provider)
    try:
        view = _compute_view(args, session, trial_yaml=args.trial_yaml)
        payload = preview_payload(view)
        payload["ok"] = True
        if args.json:
            _print_json(payload)
        else:
            print(render_preview_table(payload))
    finally:
        _close_provider(provider)
        ledger.close()
    return 0


def cmd_accept(args) -> int:
    provider = _make_provider(args.fixtures_dir)
    ledger, session = _open_session(args.db, provider)
    try:
        view = session.compute_preview(signal_date=args.date)
        plan = session.accept(view, actor=args.actor)
        from research.live import ManualExecutor  # 局部导入避免循环

        payload = {
            "ok": True, "plan_id": plan.plan_id, "status": plan.status,
            "signal_date": plan.signal_date, "trade_date": plan.trade_date,
            "reserved_total": round(plan.reserved_total, 2),
            "orders": [{"client_order_id": o.client_order_id,
                        "kind": o.kind, "symbol": o.symbol,
                        "planned_qty": o.planned_qty,
                        "ref_price": o.ref_price,
                        "est_value": o.est_value,
                        "budget_reserved": round(o.budget_reserved, 2),
                        "reason": o.reason}
                       for o in plan.orders],
            "instruction_sheet": ManualExecutor().instruction_sheet(plan),
        }
        _print_json(payload)
    finally:
        _close_provider(provider)
        ledger.close()
    return 0


def cmd_skip(args) -> int:
    provider = _make_provider(args.fixtures_dir)
    ledger, session = _open_session(args.db, provider)
    try:
        view = session.compute_preview(signal_date=args.date)
        plan = session.skip(view, reason=args.reason, actor=args.actor)
        _print_json({"ok": True, "plan_id": plan.plan_id,
                     "status": plan.status, "skip_reason": plan.skip_reason})
    finally:
        _close_provider(provider)
        ledger.close()
    return 0


def _store_only(db: str, fn) -> int:
    ledger = _open_ledger(db)
    try:
        store = PlanStore.attach(ledger)
        payload = fn(store)
    finally:
        ledger.close()
    payload["ok"] = True
    _print_json(payload)
    return 0


def cmd_submit(args) -> int:
    def run(store):
        order = store.record_submission(args.order, actor=args.actor,
                                        note=args.note)
        return {"client_order_id": order.client_order_id,
                "status": order.status}

    return _store_only(args.db, run)


def cmd_fill(args) -> int:
    def run(store):
        order = store.record_fill(
            args.order, date=args.date, quantity=args.qty, price=args.price,
            fee_commission=args.commission, fee_tax=args.tax,
            fee_transfer=args.transfer, fee_other=args.other,
            source=args.source, trade_id=args.trade_id or
            f"{args.order}#{args.date}#{args.qty}@{args.price}",
            actor=args.actor, note=args.note)
        return {"client_order_id": order.client_order_id,
                "status": order.status, "filled_qty": order.filled_qty,
                "avg_fill_price": order.avg_fill_price,
                "budget_reserved": order.budget_reserved}

    return _store_only(args.db, run)


def cmd_reject(args) -> int:
    def run(store):
        order = store.record_rejection(args.order, reason=args.reason,
                                       actor=args.actor)
        return {"client_order_id": order.client_order_id,
                "status": order.status}

    return _store_only(args.db, run)


def cmd_cancel_order(args) -> int:
    def run(store):
        order = store.record_cancellation(args.order, reason=args.reason,
                                          actor=args.actor)
        return {"client_order_id": order.client_order_id,
                "status": order.status, "filled_qty": order.filled_qty}

    return _store_only(args.db, run)


def cmd_unknown(args) -> int:
    def run(store):
        order = store.mark_unknown(args.order, note=args.note,
                                   actor=args.actor)
        return {"client_order_id": order.client_order_id,
                "status": order.status,
                "hint": "只能 resolve 查询核验后恢复，不能重发"}

    return _store_only(args.db, run)


def cmd_resolve(args) -> int:
    def run(store):
        fills = _load_yaml(args.fills).get("fills") if args.fills else None
        order = store.resolve_unknown(
            args.order, args.status, fills=fills, reason=args.reason,
            actor=args.actor, source=args.source)
        return {"client_order_id": order.client_order_id,
                "status": order.status, "filled_qty": order.filled_qty}

    return _store_only(args.db, run)


def cmd_expire(args) -> int:
    def run(store):
        expired = store.expire_plans(args.as_of or _today(),
                                     actor=args.actor)
        return {"expired": [{"plan_id": p.plan_id, "status": p.status}
                            for p in expired]}

    return _store_only(args.db, run)


def cmd_cancel_plan(args) -> int:
    def run(store):
        plan = store.cancel_plan(args.plan, reason=args.reason,
                                 actor=args.actor)
        return {"plan_id": plan.plan_id, "status": plan.status}

    return _store_only(args.db, run)


def cmd_plans(args) -> int:
    def run(store):
        return {"plans": [{"plan_id": p.plan_id, "status": p.status,
                           "signal_date": p.signal_date,
                           "trade_date": p.trade_date,
                           "reserved_total": p.reserved_total,
                           "skip_reason": p.skip_reason}
                          for p in store.plans(status=args.status)]}

    return _store_only(args.db, run)


def cmd_orders(args) -> int:
    def run(store):
        from research.live import _order_dict
        from research.live_plan import gate_order

        orders = store.orders(plan_id=args.plan)
        payload = {"orders": []}
        for order in orders:
            item = _order_dict(order)
            if (order.side == "BUY" and order.proceeds_alloc
                    and order.status in ("planned", "submitted", "partial",
                                         "unknown")):
                gate = gate_order(store, order.client_order_id)
                item["gate"] = {"allowed": gate.allowed,
                                "quantity": gate.quantity,
                                "reason": gate.reason}
            payload["orders"].append(item)
        return payload

    return _store_only(args.db, run)


def cmd_status(args) -> int:
    provider = None
    ledger = None
    try:
        if args.with_session:
            provider = _make_provider(args.fixtures_dir)
            ledger, session = _open_session(args.db, provider)
            payload = status_payload(session.ledger, session)
        else:
            ledger = _open_ledger(args.db, read_only=True)
            payload = status_payload(ledger)
        payload["ok"] = True
        if args.json:
            _print_json(payload)
        else:
            print(render_status_table(payload))
    finally:
        if provider is not None:
            _close_provider(provider)
        if ledger is not None:
            ledger.close()
    return 0


# ── 参数装配 ──


def _add_actor(p):
    p.add_argument("--actor", default="user", help="操作者标识（审计用）")


def _add_order_args(p):
    p.add_argument("db")
    p.add_argument("--order", required=True, help="client_order_id")
    _add_actor(p)


def _add_session_args(p):
    p.add_argument("db")
    p.add_argument("--date", default=None, help="信号日 YYYYMMDD（缺省最新）")
    p.add_argument("--fixtures-dir", default=None,
                   help="离线演练：tests fixtures 行情目录（正常实盘勿用）")
    _add_actor(p)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="实盘 V2：账户快照 / 对账 / 策略会话 / 执行计划")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="建账本（不覆盖已有文件）")
    p.add_argument("db")
    p.add_argument("--account-id", required=True)
    p.add_argument("--broker", default="")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_init)

    p = sub.add_parser("import", help="对账单导入（事实 + 快照一站式）")
    p.add_argument("db")
    p.add_argument("file", help="statement YAML（docs/live_guide.md#input）")
    p.add_argument("--source", default="broker")
    p.set_defaults(fn=cmd_import)

    p = sub.add_parser("import-fills",
                       help="成交导入（规范 CSV 或 YAML/JSON，见 live_guide）")
    p.add_argument("db")
    p.add_argument("file")
    p.add_argument("--source", default="broker")
    p.add_argument("--date", default=None,
                   help="缺省成交日（存在缺 date 行时必填）")
    p.add_argument("--batch-id", default=None, help="缺省用文件名")
    p.set_defaults(fn=cmd_import_fills)

    p = sub.add_parser("confirm", help="确认候选快照（重算差异或接管）")
    p.add_argument("db")
    p.add_argument("--snapshot", type=int, required=True)
    p.add_argument("--takeover", action="store_true",
                   help="显式接管（差异保留为 PnL 缺口）")
    p.add_argument("--reason", default="", help="接管原因（takeover 必填）")
    p.set_defaults(fn=cmd_confirm)

    p = sub.add_parser("cashflow", help="分类现金流（入金/费用/分红等）")
    p.add_argument("db")
    p.add_argument("--category", required=True,
                   choices=["deposit", "withdrawal", "fee", "dividend",
                            "tax", "explicit_adjustment"])
    p.add_argument("--amount", type=float, required=True, help="带符号金额")
    p.add_argument("--date", default=None)
    p.add_argument("--symbol", default="")
    p.add_argument("--source", default="user")
    p.add_argument("--event-id", default="")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_cashflow)

    p = sub.add_parser("adjust-shares", help="股数调整（送转/手动更正）")
    p.add_argument("db")
    p.add_argument("--symbol", required=True)
    p.add_argument("--delta", type=int, required=True)
    p.add_argument("--category", default="corporate_action",
                   choices=["corporate_action", "manual"])
    p.add_argument("--reason", required=True)
    p.add_argument("--date", default=None)
    p.add_argument("--source", default="user")
    p.add_argument("--event-id", default="")
    p.set_defaults(fn=cmd_adjust_shares)

    p = sub.add_parser("policy", help="持仓管理政策")
    p.add_argument("db")
    p.add_argument("--symbol", required=True)
    p.add_argument("--policy", choices=["managed", "exit_only", "observe"])
    p.add_argument("--reason", default="")
    p.add_argument("--anchor-date", default=None)
    p.add_argument("--anchor-price", type=float, default=None)
    p.add_argument("--expire-date", default=None)
    p.add_argument("--clear", action="store_true", help="恢复默认 observe")
    p.set_defaults(fn=cmd_policy)

    p = sub.add_parser("activate", help="启用策略（版本固定，正式生效）")
    _add_session_args(p)
    p.add_argument("--yaml", required=True, help="策略 YAML 路径")
    p.add_argument("--anchors",
                   help="锚点 YAML {anchors: {symbol: {entry_date, entry_price}}}")
    p.add_argument("--no-manage", action="store_true",
                   help="不为锚点持仓登记 managed 政策")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_activate)

    p = sub.add_parser("preview", help="只读预览次日操作单（不写库）")
    _add_session_args(p)
    p.add_argument("--trial-yaml", default=None, help="试算另一策略")
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_preview)

    p = sub.add_parser("accept", help="接受当前预览 → 计划落库")
    _add_session_args(p)
    p.set_defaults(fn=cmd_accept)

    p = sub.add_parser("skip", help="跳过当前决策（原因必填）")
    _add_session_args(p)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_skip)

    p = sub.add_parser("submit", help="人工已按操作单提交")
    _add_order_args(p)
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_submit)

    p = sub.add_parser("fill", help="成交回报")
    _add_order_args(p)
    p.add_argument("--date", required=True)
    p.add_argument("--qty", type=int, required=True)
    p.add_argument("--price", type=float, required=True)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--tax", type=float, default=0.0)
    p.add_argument("--transfer", type=float, default=0.0)
    p.add_argument("--other", type=float, default=0.0)
    p.add_argument("--trade-id", default=None, help="券商成交编号（优先）")
    p.add_argument("--source", default="user")
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_fill)

    p = sub.add_parser("reject", help="拒单回报")
    _add_order_args(p)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_reject)

    p = sub.add_parser("cancel-order", help="撤单回报（剩余作废）")
    _add_order_args(p)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_cancel_order)

    p = sub.add_parser("unknown", help="提交结果未知（等待查询核验）")
    _add_order_args(p)
    p.add_argument("--note", default="")
    p.set_defaults(fn=cmd_unknown)

    p = sub.add_parser("resolve", help="unknown 查询核验恢复")
    _add_order_args(p)
    p.add_argument("--status", required=True,
                   choices=["submitted", "partial", "filled", "cancelled",
                            "rejected"])
    p.add_argument("--reason", default="")
    p.add_argument("--fills", default=None,
                   help="成交明细 YAML {fills: [...]}（filled/partial 用）")
    p.add_argument("--source", default="query")
    p.set_defaults(fn=cmd_resolve)

    p = sub.add_parser("expire", help="过期未执行计划（释放预留）")
    p.add_argument("db")
    p.add_argument("--as-of", default=None)
    _add_actor(p)
    p.set_defaults(fn=cmd_expire)

    p = sub.add_parser("cancel-plan", help="取消计划（须无在途订单）")
    p.add_argument("db")
    p.add_argument("--plan", required=True)
    p.add_argument("--reason", required=True)
    _add_actor(p)
    p.set_defaults(fn=cmd_cancel_plan)

    p = sub.add_parser("plans", help="计划列表")
    p.add_argument("db")
    p.add_argument("--status", default=None)
    p.set_defaults(fn=cmd_plans)

    p = sub.add_parser("orders", help="订单列表")
    p.add_argument("db")
    p.add_argument("--plan", default=None)
    p.set_defaults(fn=cmd_orders)

    p = sub.add_parser("status", help="账户状态（默认只读，不含会话）")
    p.add_argument("db")
    p.add_argument("--with-session", action="store_true",
                   help="附启用策略与在途订单（需要行情）")
    p.add_argument("--fixtures-dir", default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except LiveAccountError as exc:
        code = getattr(exc, "code", "live")
        _error(code, str(exc))
        return 1
    except (OSError, ValueError) as exc:
        _error("validation", str(exc))
        return 2


if __name__ == "__main__":
    sys.exit(main())
