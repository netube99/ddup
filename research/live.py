"""实盘 V2 应用门面 — 组合账户/计划/会话三层，供 CLI 与离线演练消费。

职责边界（docs/live_redesign.md §2）：
- 本模块不做领域决策：账户事实在 live_account，计划/执行在 live_plan，
  策略会话在 live_strategy。这里只做编排（对账单导入、状态汇总、
  人类可读渲染），保持 CLI 薄壳可测。
- 旧 V1 账本（ledger_fills 回放重写 runs/trade_log 派生表）已停用：
  V2 账本 schema_version=2，旧库只读识别后明确拒绝，不写坏旧文件。

对账单导入顺序（每日闭环）：先入账成交/现金流/股数调整（事实），
再导入券商快照——投影已包含当日事实，差异即"账本未解释的差额"：
clean 自动确认；有差异存候选，由用户分类（record_cash_flow）后
reconcile 或显式接管（takeover），未确认前不偷换正式状态。
"""

import json
from pathlib import Path

from research.live_account import (  # noqa: F401  (公共门面再导出)
    POLICY_EXIT_ONLY,
    POLICY_MANAGED,
    POLICY_OBSERVE,
    AccountLedger,
    AccountValidationError,
    LiveAccountError,
)
from research.live_plan import (  # noqa: F401
    ManualExecutor,
    PlanStore,
    SimulatedExecutor,
)
from research.live_strategy import StrategySession  # noqa: F401

_CASH_CATEGORIES = frozenset({
    "deposit", "withdrawal", "fee", "dividend", "tax", "explicit_adjustment",
})
_SHARE_CATEGORIES = frozenset({"corporate_action", "manual"})


def _finite(value, field: str) -> float:
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise AccountValidationError(f"{field} 非法（NaN/Inf）: {value!r}")
    return number


def import_statement(ledger: AccountLedger, statement: dict, *,
                     source: str = "broker") -> dict:
    """对账单一站式导入：现金流/股数调整/成交 → 快照，返回各步结果。

    statement（YAML/JSON 解析后的 dict）：
      date: YYYYMMDD（快照 as_of；非交易日请先归一）
      cash: 数值（余额=可用，冻结 0）或 cash_balance/cash_available
      holdings: [{symbol, shares, available?}]（必填，空仓传 []）
      fills: [{date?, symbol, side, quantity, price, trade_id?,
               fee_commission?, fee_tax?, fee_transfer?, fee_other?, note?}]
      cash_flows: [{date?, category, amount, symbol?, note?}]
      share_adjustments: [{date?, symbol, delta_shares, category, reason}]
      import_id: 缺省用 "{source}:{date}"（同身份同内容幂等）
    """
    if not isinstance(statement, dict):
        raise AccountValidationError(
            f"statement 必须是映射: {type(statement).__name__}")
    date = str(statement.get("date") or "")
    if len(date) != 8 or not date.isdigit():
        raise AccountValidationError(
            f"statement.date 需为 YYYYMMDD: {date!r}")
    import_id = str(statement.get("import_id") or f"{source}:{date}")

    flows_in = statement.get("cash_flows") or []
    adj_in = statement.get("share_adjustments") or []
    fills_in = statement.get("fills") or []
    for label, items in (("cash_flows", flows_in),
                         ("share_adjustments", adj_in),
                         ("fills", fills_in)):
        if not isinstance(items, list):
            raise AccountValidationError(f"statement.{label} 必须是列表")

    cash_balance = statement.get("cash_balance")

    def _record_facts():
        """成交/现金流/股数调整依次入账；返回各步结果。"""
        flow_results = []
        for i, row in enumerate(flows_in, start=1):
            category = str(row.get("category") or "")
            if category not in _CASH_CATEGORIES:
                raise AccountValidationError(
                    f"cash_flows 第 {i} 行 category 非法 {category!r}"
                    f"（允许 {sorted(_CASH_CATEGORIES)}）")
            flow = ledger.record_cash_flow(
                date=str(row.get("date") or date), category=category,
                amount=_finite(row.get("amount"),
                               f"cash_flows 第 {i} 行 amount"),
                symbol=str(row.get("symbol") or ""), source=source,
                event_id=str(row.get("event_id") or f"{import_id}:cf{i}"),
                note=str(row.get("note") or ""))
            flow_results.append(flow)

        adj_results = []
        for i, row in enumerate(adj_in, start=1):
            category = str(row.get("category") or "")
            if category not in _SHARE_CATEGORIES:
                raise AccountValidationError(
                    f"share_adjustments 第 {i} 行 category 非法 {category!r}"
                    f"（允许 {sorted(_SHARE_CATEGORIES)}）")
            adj = ledger.record_share_adjustment(
                date=str(row.get("date") or date), symbol=str(row["symbol"]),
                delta_shares=int(_finite(row["delta_shares"], "delta_shares")),
                category=category, reason=str(row.get("reason") or ""),
                source=source,
                event_id=str(row.get("event_id") or f"{import_id}:sa{i}"))
            adj_results.append(adj)

        fills_result = None
        if fills_in:
            fills_result = ledger.import_fills(
                source, f"{import_id}", fills_in, default_date=date,
                note="statement import")
        return flow_results, adj_results, fills_result

    cash_available = statement.get("cash_available")
    cash = statement.get("cash")
    if cash is not None and (cash_balance is not None
                             or cash_available is not None):
        raise AccountValidationError(
            "cash 与 cash_balance/cash_available 只能提供一组")
    snapshot_kwargs = {}
    if cash is not None:
        snapshot_kwargs["cash"] = _finite(cash, "cash")
    else:
        if cash_balance is None:
            raise AccountValidationError("statement 缺 cash（或 cash_balance）")
        snapshot_kwargs["cash_balance"] = _finite(cash_balance, "cash_balance")
        if cash_available is not None:
            snapshot_kwargs["cash_available"] = _finite(cash_available,
                                                        "cash_available")

    holdings = statement.get("holdings")
    if holdings is None:
        raise AccountValidationError("statement.holdings 必填（空仓传 []）")
    normalized_holdings = []
    for i, row in enumerate(holdings, start=1):
        if not isinstance(row, dict):
            raise AccountValidationError(f"holdings 第 {i} 行非映射: {row!r}")
        for key in ("symbol", "shares"):
            if row.get(key) is None:
                raise AccountValidationError(f"holdings 第 {i} 行缺 {key}: {row!r}")
        item = {"symbol": str(row["symbol"]),
                "shares": int(_finite(row["shares"],
                                      f"holdings 第 {i} 行 shares"))}
        if row.get("available") is not None:
            item["available"] = int(_finite(row["available"], "available"))
        if row.get("cost") is not None:
            item["cost"] = _finite(row["cost"], "cost")
        normalized_holdings.append(item)

    def _import_snapshot():
        return ledger.import_snapshot(
            as_of=date, source=source, import_id=import_id,
            holdings=normalized_holdings, raw=json.dumps(
                statement, ensure_ascii=False, default=str), **snapshot_kwargs)

    if ledger.confirmed_snapshot() is None:
        # 首份对账单：券商日终状态即开仓基准（opening）；其携带的事实
        # （成交/入金）随后入账——日期 ≤ 基准日，仅作审计不重复投影
        snapshot = _import_snapshot()
        flow_results, adj_results, fills_result = _record_facts()
    else:
        # 后续对账单：先入事实再导快照，差异即"账本未解释的差额"，
        # clean 自动确认，否则存候选等用户分类/接管
        flow_results, adj_results, fills_result = _record_facts()
        snapshot = _import_snapshot()

    return {"snapshot": snapshot, "fills": fills_result,
            "cash_flows": flow_results, "share_adjustments": adj_results}


def import_fills_file(ledger: AccountLedger, path: str, *,
                      source: str = "broker", default_date: str | None = None,
                      batch_id: str | None = None):
    """成交导入：规范 CSV 或 YAML/JSON {fills: [...]}（LIVE-V2-01）。

    CSV 规范（首行表头必需，列名固定，选到列可缺）：
      trade_id?,date?,symbol,side,quantity,price,
      fee_commission?,fee_tax?,fee_transfer?,fee_other?,note?
    行缺 date 时用 default_date（必填否则拒绝）；缺 trade_id 时按
    "{batch}#r{行号}" 生成。不冒充兼容所有券商导出——异构列名请先转换。
    """
    import csv

    suffix = Path(path).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        payload = _load_yaml_map(path)
        rows = payload.get("fills")
    elif suffix == ".json":
        rows = json.loads(Path(path).read_text(encoding="utf-8")).get("fills")
    elif suffix == ".csv":
        with open(path, encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            rows = []
            for i, raw in enumerate(reader, start=1):
                row = {k: v for k, v in raw.items()
                       if k is not None and v is not None and str(v).strip() != ""}
                for key in ("symbol", "side", "quantity", "price"):
                    if key not in row:
                        raise AccountValidationError(
                            f"CSV 第 {i} 行缺必需列 {key}: {raw!r}")
                rows.append(row)
    else:
        raise AccountValidationError(
            f"不支持的成交文件格式 {suffix!r}（支持 .csv/.yaml/.json）")
    if not isinstance(rows, list):
        raise AccountValidationError("成交文件需是 {fills: [...]} 或 CSV 表")
    if rows and any(str(r.get("date") or "").strip() == ""
                    for r in rows) and not default_date:
        raise AccountValidationError(
            "存在缺 date 的成交行且未提供 --date 缺省成交日")
    return ledger.import_fills(source, batch_id or Path(path).name, rows,
                               default_date=default_date,
                               note="fills file import")


def _load_yaml_map(path: str) -> dict:
    import yaml

    if not Path(path).is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise AccountValidationError(f"YAML 顶层必须是映射: {path}")
    return payload


def status_payload(ledger: AccountLedger,
                   session: StrategySession | None = None) -> dict:
    """status 命令载荷：确认基准、未决差异、投影、政策、启用与在途订单。"""
    status = ledger.status()
    payload = {
        "account_id": ledger.account_id,
        "broker": ledger.broker,
        "history_incomplete": status.history_incomplete,
        "confirmed": (_snapshot_dict(status.confirmed_snapshot)
                      if status.confirmed_snapshot else None),
        "pending_candidates": [
            {"snapshot_id": c.snapshot_id, "as_of": c.as_of,
             "source": c.source, "import_id": c.import_id,
             "diffs": _diff_dict(c.diff)}
            for c in status.pending],
    }
    if status.projected is not None:
        payload["projection"] = {
            "base_as_of": status.projected.base_as_of,
            "cash": round(status.projected.cash, 2),
            "cash_available": round(status.projected.cash_available, 2),
            "holdings": {s: h.shares for s, h in
                         sorted(status.projected.holdings.items())},
        }
        payload["policies"] = ledger.effective_policies()
    if session is not None:
        activation = session.active_activation()
        if activation is not None:
            payload["activation"] = {
                "activation_id": activation.activation_id,
                "effective_date": activation.effective_date,
                "strategy": activation.identity.name,
                "config_hash": activation.identity.config_hash[:12],
                "status": activation.status,
            }
        plans = session.plans
        payload["outstanding_reserved"] = round(
            plans.outstanding_reserved(), 2)
        open_orders = [o for o in plans.orders()
                       if o.status in ("planned", "submitted", "partial",
                                       "unknown")]
        payload["open_orders"] = [_order_dict(o) for o in open_orders]
        active_plans = plans.plans(status="active") + plans.plans(
            status="partial")
        payload["active_plans"] = [
            {"plan_id": p.plan_id, "status": p.status,
             "signal_date": p.signal_date, "trade_date": p.trade_date,
             "expiry_date": p.expiry_date}
            for p in active_plans]
    return payload


def _snapshot_dict(snapshot) -> dict:
    return {"snapshot_id": snapshot.snapshot_id, "as_of": snapshot.as_of,
            "source": snapshot.source, "import_id": snapshot.import_id,
            "status": snapshot.status,
            "cash_balance": snapshot.cash_balance,
            "cash_available": snapshot.cash_available,
            "holdings": {s: h.shares for s, h in
                         sorted(snapshot.holdings.items())}}


def _diff_dict(diff) -> dict:
    """SnapshotDiff → JSON 载荷（expected=账本投影，actual=券商）。"""
    if diff is None:
        return {}
    cash = diff.cash
    return {"cash": ({"expected": round(cash.expected, 2),
                      "actual": round(cash.actual, 2),
                      "delta": round(cash.delta, 2)}
                     if cash is not None else None),
            "holdings": {s: {"expected": d.expected, "actual": d.actual,
                             "delta": d.delta}
                         for s, d in sorted(diff.holding_diffs.items())}}


def _order_dict(order) -> dict:
    return {
        "client_order_id": order.client_order_id, "plan_id": order.plan_id,
        "kind": order.kind, "symbol": order.symbol, "side": order.side,
        "status": order.status, "planned_qty": order.planned_qty,
        "filled_qty": order.filled_qty, "ref_price": order.ref_price,
        "budget_reserved": round(order.budget_reserved, 2),
        "reason": order.reason,
        "depends_on": sorted(order.proceeds_alloc),
        "warnings": list(order.warnings),
    }


def preview_payload(view) -> dict:
    """预览命令载荷：决策输入版本 + 订单草稿 + 跳过/警告 + 可读操作行。"""
    return {
        "is_trial": view.is_trial,
        "activation_id": view.activation_id,
        "snapshot_id": view.snapshot_id,
        "signal_date": view.signal_date,
        "trade_date": view.trade_date,
        "expiry_date": view.expiry_date,
        "decision_hash": view.decision_hash,
        "replay_days": view.replay_days,
        "total_value": round(view.total_value, 2),
        "visible_holdings": dict(view.visible_holdings),
        "actions": view.actions,
        "orders": [o.canonical() | {"seq": o.seq} for o in view.orders.orders],
        "skipped": [dict(s) for s in view.orders.skipped],
        "warnings": list(view.warnings),
        "budget": {
            "cash_available": round(view.orders.budget.cash_available, 2),
            "outstanding_reserved": round(
                view.orders.budget.outstanding_reserved, 2),
            "blocked_reason": view.orders.budget.blocked_reason,
        },
        "instruction_sheet": [
            {"client_order_id": row["client_order_id"],
             "action": row["action"], "symbol": row["symbol"],
             "quantity": row["quantity"], "ref_price": row["ref_price"],
             "trigger_price": row["trigger_price"],
             "est_value": row["est_value"], "reason": row["reason"],
             "depends_on": row["depends_on"], "notes": row["notes"]}
            for row in ManualExecutor().instruction_sheet_from(view.orders)],
    }


def render_status_table(payload: dict) -> str:
    """人类可读 status 概览（CLI 表格输出）。"""
    lines = []
    confirmed = payload.get("confirmed")
    if confirmed:
        lines.append(f"确认基准: snapshot {confirmed['snapshot_id']}"
                     f" @ {confirmed['as_of']}（{confirmed['source']}"
                     f"/{confirmed['import_id']}）")
        lines.append(f"现金: 余额 {confirmed['cash_balance']:.2f}"
                     f" / 可用 {confirmed['cash_available']:.2f}")
        holdings = confirmed["holdings"]
        lines.append("持仓: " + (", ".join(
            f"{s}×{n}" for s, n in holdings.items()) if holdings else "空仓"))
    else:
        lines.append("确认基准: 无（先导入并确认快照）")
    pending = payload.get("pending_candidates") or []
    if pending:
        lines.append(f"待确认差异: {len(pending)} 笔候选"
                     f"（最新 snapshot {pending[-1]['snapshot_id']}"
                     f" @ {pending[-1]['as_of']}）")
    projection = payload.get("projection") or {}
    if projection:
        lines.append(f"投影: 现金 {projection['cash']:.2f}"
                     f"（可用 {projection['cash_available']:.2f}），"
                     f"持仓 {len(projection['holdings'])} 只")
    policies = payload.get("policies") or {}
    observe = sorted(s for s, p in policies.items() if p == POLICY_OBSERVE)
    if observe:
        lines.append(f"observe（人工保留）: {', '.join(observe)}")
    activation = payload.get("activation")
    if activation:
        lines.append(f"启用策略: {activation['strategy']}"
                     f" @ {activation['effective_date']}"
                     f"（activation {activation['activation_id']}，"
                     f"{activation['status']}）")
    reserved = payload.get("outstanding_reserved")
    if reserved is not None:
        lines.append(f"预算预留（在途订单）: {reserved:.2f}")
    open_orders = payload.get("open_orders") or []
    if open_orders:
        lines.append("在途订单:")
        for o in open_orders:
            lines.append(
                f"  {o['client_order_id']}  {o['action'] if 'action' in o else o['kind']}"
                f" {o['symbol']} {o['filled_qty']}/{o['planned_qty']} 股"
                f" [{o['status']}] {o['reason']}")
    return "\n".join(lines)


def render_preview_table(payload: dict) -> str:
    """人类可读操作单（CLI 表格输出）。"""
    lines = [
        f"信号日 {payload['signal_date']} → 执行日 {payload['trade_date']}"
        f"（decision {payload['decision_hash'][:12]}…，"
        f"总资产 {payload['total_value']:.2f}）",
    ]
    budget = payload.get("budget") or {}
    if budget.get("blocked_reason"):
        lines.append(f"预算阻止: {budget['blocked_reason']}")
    sheet = payload.get("instruction_sheet") or []
    if sheet:
        lines.append("操作单:")
        for row in sheet:
            trigger = (f" 触发价 {row['trigger_price']:.2f}"
                       if row.get("trigger_price") else "")
            depends = (f"（依赖 {','.join(row['depends_on'])} 先成交）"
                       if row.get("depends_on") else "")
            lines.append(
                f"  {row['action']} {row['symbol']} {row['quantity']} 股"
                f" @参考 {row['ref_price']:.2f}{trigger}"
                f" ≈{row['est_value']:.0f} 元 [{row['reason']}]{depends}")
            for note in row["notes"]:
                lines.append(f"    - {note}")
    else:
        lines.append("操作单: 无订单（策略无动作或全部被跳过）")
    skipped = payload.get("skipped") or []
    if skipped:
        lines.append("跳过: " + "; ".join(
            f"{s['symbol']}（{s['reason']}）" for s in skipped))
    warnings = payload.get("warnings") or []
    for warning in warnings:
        lines.append(f"警告: {warning}")
    return "\n".join(lines)
