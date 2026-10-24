"""实盘 V2 计划与执行层 — docs/live_redesign.md §5。

职责（计划、预算与执行）：
- 计划内容（§5.1）：不可变 plan_id/revision、账户快照版本、策略版本、
  signal/trade/expiry、动作、理由、预算、参考价、数量、依赖与警告；
  新计划显式 supersede 旧计划；接受幂等 + 版本检查；预算预留与实际现金
  分别记录——账户 facts 不因接受计划而提前改变，成交只在执行回报时入账。
- 分配与容差（§5.2）：同批顺序预算递减；现金支出+费用缓冲不超过
  可用现金减已有预留；卖出回款只作为依赖条件，未确认成交前不解锁买单；
  target_value 统一转差额；整手买入与清仓零股；min_trade_amount /
  position_tolerance / price_deviation_limit / cash_buffer 可解释容差。
- 条件与执行适配（§5.3）：STOP_LOSS/TAKE_PROFIT/TRAILING_TP/ML_EXIT 与
  buy_conditions 的完整映射或明确 UnsupportedConditionError（不丢参数）；
  同票卖出与保护单互斥/缩减；订单状态机
  planned/submitted/partial/filled/cancelled/rejected/unknown；稳定
  client_order_id；unknown 只能查询核验后恢复，不能盲目重发。

依赖：仅账户模块（research.live_account）与 btcore.costs 估算费用；
不依赖 CLI/Engine/策略。计划表（plan_ 前缀）建在同一账本库，全部写
路径经 ``ledger.transaction()`` 组合事务：订单更新、成交入账
（ledger.record_fill）与预留释放原子完成，任一嵌套失败整体回滚。
"""

import hashlib
import json
import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

from btcore.costs import make_costs_fn
from research.live_account import (
    POLICY_EXIT_ONLY,
    POLICY_MANAGED,
    POLICY_OBSERVE,
    SIDE_BUY,
    SIDE_SELL,
    AccountLedger,
    LiveAccountError,
)
from research.live_account import (
    require_non_negative as _acct_require_non_negative,
)
from research.live_account import (
    require_positive_price as _acct_require_positive_price,
)
from research.live_account import (
    require_text as _acct_require_text,
)
from research.live_account import (
    require_whole_int as _acct_require_whole_int,
)
from research.live_account import (
    validate_date_format as _acct_validate_date_format,
)

logger = logging.getLogger(__name__)

PLAN_SCHEMA_VERSION = 1
LOT_SIZE = 100

# 订单类型
KIND_BUY = "BUY"
KIND_SELL = "SELL"
KIND_ENTRY_COND = "ENTRY_COND"
KIND_PROTECT = "PROTECT"

# 订单状态（§5.3）；open = 仍占用预算预留
ORDER_PLANNED = "planned"
ORDER_SUBMITTED = "submitted"
ORDER_PARTIAL = "partial"
ORDER_FILLED = "filled"
ORDER_CANCELLED = "cancelled"
ORDER_REJECTED = "rejected"
ORDER_UNKNOWN = "unknown"
ORDER_EXPIRED = "expired"
_ORDER_OPEN = frozenset({ORDER_PLANNED, ORDER_SUBMITTED, ORDER_PARTIAL, ORDER_UNKNOWN})

# 计划状态（§5.1：接受、跳过、过期、取消、部分完成、完成 + 显式 supersede）
PLAN_ACTIVE = "active"
PLAN_SKIPPED = "skipped"
PLAN_EXPIRED = "expired"
PLAN_CANCELLED = "cancelled"
PLAN_PARTIAL = "partial"
PLAN_COMPLETED = "completed"
PLAN_SUPERSEDED = "superseded"

# 可翻译的退出/入场条件类型（§5.3）；其余类型明确拒绝，不压成空价格
TRANSLATABLE_EXIT_CONDITIONS = frozenset(
    {"STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP", "ML_EXIT"}
)
TRANSLATABLE_ENTRY_CONDITIONS = frozenset({"LIMIT_BUY", "BREAKOUT_BUY"})


# ── 异常：code 供 CLI 映射稳定错误码 ──


class PlanError(LiveAccountError):
    code = "plan"


class PlanValidationError(PlanError):
    code = "validation"


class PlanConflictError(PlanError):
    code = "conflict"


class PlanStateError(PlanError):
    code = "state"


class UnsupportedConditionError(PlanValidationError):
    """不支持的执行语义：显式拒绝并携带完整参数，不静默丢弃。"""

    code = "unsupported"


# ── 入口校验：账户层异常统一转计划层异常（CLI 错误码稳定为 validation）──


def validate_date_format(value, name: str = "date") -> str:
    try:
        return _acct_validate_date_format(value, name)
    except LiveAccountError as exc:
        raise PlanValidationError(str(exc)) from None


def require_non_negative(value, name: str) -> float:
    try:
        return _acct_require_non_negative(value, name)
    except LiveAccountError as exc:
        raise PlanValidationError(str(exc)) from None


def require_positive_price(value, name: str) -> float:
    try:
        return _acct_require_positive_price(value, name)
    except LiveAccountError as exc:
        raise PlanValidationError(str(exc)) from None


def require_text(value, name: str) -> str:
    try:
        return _acct_require_text(value, name)
    except LiveAccountError as exc:
        raise PlanValidationError(str(exc)) from None


def require_whole_int(value, name: str, minimum: int | None = None) -> int:
    try:
        return _acct_require_whole_int(value, name, minimum=minimum)
    except LiveAccountError as exc:
        raise PlanValidationError(str(exc)) from None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _round6(value: float) -> float:
    return round(float(value), 6)


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _canonical_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


# ── 配置与视图 ──


@dataclass(frozen=True)
class ExecutionConfig:
    """执行计划的可解释容差与费用口径（§5.2）。

    容差用于减少无意义调仓/拦截明显异常回报，不用于掩盖账户矛盾。
    费率键缺省回退 btcore.constants 硬编码（与回测撮合同一工厂）。
    """

    max_positions: int = 20
    min_trade_amount: float = 0.0
    position_tolerance: float = 0.0
    price_deviation_limit: float | None = None
    cash_buffer: float = 0.0
    commission_rate: float | None = None
    min_commission: float | None = None
    stamp_tax_rate: float | None = None
    transfer_fee_rate: float | None = None

    def __post_init__(self):
        if (not isinstance(self.max_positions, int)
                or isinstance(self.max_positions, bool)
                or self.max_positions <= 0):
            raise PlanValidationError(
                f"max_positions 必须是正整数: {self.max_positions!r}")
        self.__dict__["min_trade_amount"] = require_non_negative(
            self.min_trade_amount, "min_trade_amount")
        self.__dict__["position_tolerance"] = require_non_negative(
            self.position_tolerance, "position_tolerance")
        self.__dict__["cash_buffer"] = require_non_negative(
            self.cash_buffer, "cash_buffer")
        if self.price_deviation_limit is not None:
            self.__dict__["price_deviation_limit"] = require_positive_price(
                self.price_deviation_limit, "price_deviation_limit")

    def costs_fn(self):
        rates = {
            key: getattr(self, key)
            for key in ("commission_rate", "min_commission", "stamp_tax_rate",
                        "transfer_fee_rate")
        }
        return make_costs_fn({k: v for k, v in rates.items() if v is not None})


@dataclass(frozen=True)
class BudgetContext:
    """预算上下文：可用现金、已有预留与阻止新买入的原因。

    blocked_reason 非空（待确认快照差异 / 负可用现金）时 spendable 为
    None——新买入与条件买入全部跳过，卖出不受影响；takeover_gap 属于
    收益视图不完整，不阻止已确认接管后的交易。
    """

    cash_available: float
    outstanding_reserved: float
    blocked_reason: str | None = None

    @property
    def spendable(self) -> float | None:
        if self.blocked_reason is not None:
            return None
        return max(0.0, self.cash_available - self.outstanding_reserved)


@dataclass(frozen=True)
class HoldingView:
    """build_plan 看到的持仓：股数、参考市值、生效政策与可选成本。"""

    symbol: str
    shares: int
    value: float
    policy: str = POLICY_OBSERVE
    avg_cost: float | None = None


@dataclass(frozen=True)
class AccountView:
    """计划生成的账户视图（以当前账本投影为准，非回放中间态）。"""

    snapshot_id: int
    base_as_of: str
    holdings: dict = field(default_factory=dict)  # symbol -> HoldingView
    ref_prices: dict = field(default_factory=dict)  # symbol -> 信号日收盘
    total_value: float = 0.0

    def holding(self, symbol: str) -> HoldingView | None:
        return self.holdings.get(symbol)


@dataclass(frozen=True)
class FillReport:
    """执行回报中的单笔成交。"""

    quantity: int
    price: float
    fee_commission: float = 0.0
    fee_tax: float = 0.0
    fee_transfer: float = 0.0
    fee_other: float = 0.0


@dataclass
class PlannedOrder:
    """计划内订单（未持久化前的草稿形态）。"""

    seq: int
    kind: str
    symbol: str
    side: str
    planned_qty: int
    ref_price: float
    est_value: float
    est_fees: float
    budget_reserved: float = 0.0
    proceeds_alloc: dict = field(default_factory=dict)  # sell client seq -> 金额
    reason: str = ""
    detail: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def canonical(self) -> dict:
        return {
            "kind": self.kind, "symbol": self.symbol, "side": self.side,
            "qty": self.planned_qty, "ref_price": _round6(self.ref_price),
            "est_value": _round6(self.est_value),
            "est_fees": _round6(self.est_fees),
            "budget_reserved": _round6(self.budget_reserved),
            "proceeds_alloc": {k: _round6(v) for k, v in self.proceeds_alloc.items()},
            "reason": self.reason, "detail": self.detail,
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class PlanDraft:
    """build_plan 的只读产物：订单、跳过项、警告与预算快照。"""

    orders: tuple
    skipped: tuple  # {"symbol": ..., "reason": ...}
    warnings: tuple
    budget: BudgetContext
    mode: str  # target_value | lists | conditions

    def fingerprint(self) -> str:
        payload = {
            "orders": [o.canonical() for o in self.orders],
            "skipped": [dict(s) for s in self.skipped],
            "warnings": list(self.warnings),
            "mode": self.mode,
        }
        return _sha1(_canonical_json(payload))


@dataclass(frozen=True)
class PlanOrder:
    """持久化订单行。"""

    order_id: int
    plan_id: str
    client_order_id: str
    seq: int
    kind: str
    symbol: str
    side: str
    reason: str
    planned_qty: int
    ref_price: float
    est_value: float
    est_fees: float
    budget_reserved: float
    proceeds_alloc: dict
    detail: dict
    warnings: tuple
    status: str
    filled_qty: int
    avg_fill_price: float | None
    filled_fees: float
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Plan:
    """持久化计划（含订单）。"""

    plan_id: str
    revision: int
    activation_id: int
    snapshot_id: int
    signal_date: str
    trade_date: str
    expiry_date: str
    status: str
    decision_hash: str
    decision_json: str
    skip_reason: str
    cash_available_at_accept: float
    reserved_total: float
    created_at: str
    accepted_at: str
    superseded_by: str
    orders: tuple = ()

    @property
    def open_orders(self) -> tuple:
        return tuple(o for o in self.orders if o.status in _ORDER_OPEN)


@dataclass(frozen=True)
class ExecutionOutcome:
    """执行器输出：提交/查询的结果与成交明细。"""

    client_order_id: str
    status: str  # submitted|rejected|unknown|partial|filled|cancelled
    fills: tuple = ()  # FillReport
    reason: str = ""


# ── 卖出回款池：未确认成交只作依赖条件（§5.2） ──


class _ProceedsPool:
    """按卖出订单序号跟踪"未确认净回款"的依赖分配。

    买单资金分解为现金部分（当即预留）与回款部分（depends 卖出单，
    只有实际成交回报才释放相应预算——A11 部分回款只释放相应份额）。
    """

    def __init__(self, sell_orders: list):
        self._available = {f"#{o.seq:02d}": o.est_value - o.est_fees
                           for o in sell_orders}
        self._order_of = {f"#{o.seq:02d}": o for o in sell_orders}

    @property
    def total(self) -> float:
        return sum(self._available.values())

    def allocate(self, need: float) -> dict:
        """瀑布分配：按卖单序号顺序占用净回款，返回 {卖单序号: 金额}。"""
        alloc: dict[str, float] = {}
        remaining = need
        for cid in sorted(self._available):
            if remaining <= 1e-9:
                break
            take = min(remaining, self._available[cid])
            if take > 1e-9:
                alloc[cid] = take
                remaining -= take
        return alloc

    def commit(self, alloc: dict) -> None:
        for cid, amount in alloc.items():
            self._available[cid] -= amount

    def release(self, alloc: dict) -> None:
        for cid, amount in alloc.items():
            self._available[cid] += amount


# ── 计划生成（纯函数） ──


def _lot_floor(amount: float, price: float) -> int:
    if price <= 0:
        return 0
    return int(amount / price / LOT_SIZE) * LOT_SIZE


def _est_fees(costs_fn, side: str, turnover: float) -> float:
    if turnover <= 0:
        return 0.0
    fees = costs_fn(side, turnover)
    return fees["commission"] + fees["stamp_tax"] + fees["transfer_fee"]


def build_plan(actions: dict, holding_conditions: dict, account: AccountView,
               budget: BudgetContext, config: ExecutionConfig) -> PlanDraft:
    """把 select() 动作与持仓条件转换为带预算/依赖/容差的订单草稿。

    输入 actions 已由引擎 select 协议校验（buy/sell/sell_shares/
    sell_reasons/buy_weights/target_value/buy_conditions 及互斥规则）；
    本函数再叠加实盘约束：持仓政策、顺序预算递减、卖后买依赖、整手、
    容差与最小金额。全部迭代按 symbol 排序保证确定性。
    """
    if not isinstance(actions, dict):
        raise PlanValidationError(f"actions 必须是 dict: {type(actions).__name__}")
    if not isinstance(holding_conditions, dict):
        raise PlanValidationError("holding_conditions 必须是 dict")
    costs_fn = config.costs_fn()
    warnings: list[str] = []
    skipped: list[dict] = []
    orders: list[PlannedOrder] = []
    seq = 0

    targets = actions.get("target_value") or {}
    sell_list = actions.get("sell") or []
    sell_shares = actions.get("sell_shares") or {}
    sell_reasons = actions.get("sell_reasons") or {}
    buy_weights = actions.get("buy_weights")
    buy_conditions = actions.get("buy_conditions") or []
    mode = "target_value" if targets else (
        "conditions" if buy_conditions else "lists")

    def ref_price_or_skip(symbol: str, purpose: str) -> float | None:
        price = account.ref_prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            skipped.append({"symbol": symbol,
                            "reason": f"{purpose}缺参考价（无行情），阻止相关决策"})
            return None
        return price

    # ── 卖出（MANUAL / TARGET：不占现金预算，估算净回款供依赖分配）──
    sell_orders: list[PlannedOrder] = []
    sell_qty_by_symbol: dict[str, int] = {}

    def emit_sell(symbol: str, qty: int, reason: str, detail: dict,
                  full_exit: bool) -> None:
        nonlocal seq
        holding = account.holding(symbol)
        if holding is None:
            skipped.append({"symbol": symbol, "reason": "卖出未持仓标的，跳过"})
            return
        if holding.policy == POLICY_OBSERVE:
            warnings.append(
                f"{symbol} 为 observe 持仓（人工保留），策略卖出指令被丢弃；"
                "该持仓仍计资产与资金占用")
            return
        if qty > holding.shares:
            warnings.append(
                f"{symbol} 卖出 {qty} 股超过投影持仓 {holding.shares} 股，按持仓截断")
            qty = holding.shares
        price = ref_price_or_skip(symbol, "卖出")
        if price is None:
            return
        if qty <= 0:
            skipped.append({"symbol": symbol, "reason": "卖出股数不足，跳过"})
            return
        turnover = qty * price
        fees = _est_fees(costs_fn, SIDE_SELL, turnover)
        seq += 1
        order = PlannedOrder(
            seq=seq, kind=KIND_SELL, symbol=symbol, side=SIDE_SELL,
            planned_qty=qty, ref_price=price, est_value=turnover,
            est_fees=fees, reason=reason,
            detail=dict(detail, full_exit=bool(full_exit)))
        sell_orders.append(order)
        orders.append(order)
        sell_qty_by_symbol[symbol] = sell_qty_by_symbol.get(symbol, 0) + qty

    if targets:
        for symbol in sorted(targets):
            target = float(targets[symbol])
            holding = account.holding(symbol)
            policy = holding.policy if holding else POLICY_OBSERVE
            price = ref_price_or_skip(symbol, "调仓")
            if price is None:
                continue
            if policy == POLICY_OBSERVE:
                warnings.append(
                    f"{symbol} 为 observe 持仓，target_value 指令被丢弃（仓位保持）")
                continue
            current = holding.shares * price if holding else 0.0
            diff = target - current
            if target > 0 and abs(diff) <= config.position_tolerance:
                skipped.append({"symbol": symbol, "reason": (
                    f"目标 {target:.2f} 与现值 {current:.2f} 差异在容差 "
                    f"{config.position_tolerance:.2f} 内，不调仓")})
                continue
            if diff < 0 and holding is not None:
                if target <= 0:
                    emit_sell(symbol, holding.shares, "TARGET",
                              {"target_value": target}, full_exit=True)
                else:
                    qty = min(_lot_floor(-diff, price), holding.shares)
                    if qty <= 0:
                        skipped.append({"symbol": symbol, "reason": (
                            f"减仓金额不足一手（参考价 {price}），跳过")})
                    else:
                        emit_sell(symbol, qty, "TARGET",
                                  {"target_value": target}, full_exit=False)
    else:
        for symbol in sorted(sell_list):
            holding = account.holding(symbol)
            qty = sell_shares.get(symbol)
            full_exit = qty is None
            if qty is None:
                qty = holding.shares if holding else 0
            reason = sell_reasons.get(symbol, "MANUAL")
            emit_sell(symbol, qty, reason, {}, full_exit=full_exit)

    pool = _ProceedsPool(sell_orders)

    # ── 买入请求（target 差额 / 权重 / 等权）──
    # buy_request: (symbol, desired_amount, reason, detail, equal_weight)
    buy_requests: list[tuple] = []
    if targets:
        for symbol in sorted(targets):
            target = float(targets[symbol])
            holding = account.holding(symbol)
            policy = holding.policy if holding else POLICY_OBSERVE
            price = ref_price_or_skip(symbol, "调仓")
            if price is None:
                continue
            current = holding.shares * price if holding else 0.0
            diff = target - current
            if diff <= 0 or (target > 0
                             and abs(diff) <= config.position_tolerance):
                continue
            if holding is not None and policy == POLICY_OBSERVE:
                warnings.append(
                    f"{symbol} 为 observe 持仓，target_value 加仓指令被丢弃")
                continue
            if holding is not None and policy == POLICY_EXIT_ONLY:
                warnings.append(
                    f"{symbol} 为 exit_only 持仓，禁止加仓；加仓指令被丢弃")
                continue
            buy_requests.append((symbol, diff, "TARGET", {"target_value": target},
                                 False))
    elif buy_weights is not None:
        for symbol in sorted(buy_weights):
            amount = account.total_value * float(buy_weights[symbol])
            buy_requests.append(
                (symbol, amount, "MANUAL", {"weight": buy_weights[symbol]}, False))
    else:
        base_amount = account.total_value / config.max_positions
        for symbol in sorted(actions.get("buy") or []):
            buy_requests.append((symbol, base_amount, "MANUAL", {}, True))

    n_positions = len(account.holdings)
    spendable = budget.spendable

    def emit_buy(symbol: str, desired: float, reason: str, detail: dict,
                 equal_weight: bool, n_left: int, kind: str,
                 trigger_price: float | None, fixed_qty: int | None = None,
                 contingent: bool = False) -> None:
        """资金分解 + 整手取整 + 预算递减；现金部分预留、缺口走回款依赖。

        contingent=True（条件买入）：触发才占用资金，数量按策略请求完整
        保留（fixed_qty 不缩量），预留取剩余可预留现金，不足时警告并在
        执行时按实际资金核验——不与即期买入抢预算。
        """
        nonlocal seq
        price = trigger_price if trigger_price is not None else \
            account.ref_prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            skipped.append({"symbol": symbol,
                            "reason": f"{kind}缺参考价（无行情），阻止相关决策"})
            return
        holding = account.holding(symbol)
        if holding is None and n_positions >= config.max_positions:
            warnings.append(
                f"持仓数已达 max_positions={config.max_positions}，"
                f"继续新买 {symbol}（与回测口径一致，不拦截）")
        if holding is not None and holding.policy == POLICY_OBSERVE:
            warnings.append(f"{symbol} 为 observe 持仓，买入指令被丢弃")
            return
        if holding is not None and holding.policy == POLICY_EXIT_ONLY:
            warnings.append(f"{symbol} 为 exit_only 持仓，禁止加仓，买入指令被丢弃")
            return
        cash_limit = remaining[0]
        qty = fixed_qty if fixed_qty is not None else _lot_floor(desired, price)
        turnover = qty * price
        if qty < LOT_SIZE or turnover < config.min_trade_amount:
            skipped.append({"symbol": symbol, "reason": (
                f"买入金额不足（可落实金额 {desired:.2f}，参考价 {price}，"
                f"min_trade_amount={config.min_trade_amount:.2f}），跳过")})
            return
        fees = _est_fees(costs_fn, SIDE_BUY, turnover)
        need = turnover + fees
        if contingent:
            reserve = min(need, cash_limit)
            if reserve < need - 1e-9:
                warnings.append(
                    f"{symbol} 条件买入 {qty} 股需 {need:.2f}，剩余可预留现金"
                    f" {cash_limit:.2f}——触发时按实际可用资金核验，多单同日"
                    "触发按订单顺序执行")
            remaining[0] -= reserve
            seq += 1
            orders.append(PlannedOrder(
                seq=seq, kind=kind, symbol=symbol, side=SIDE_BUY,
                planned_qty=qty, ref_price=price, est_value=turnover,
                est_fees=fees, budget_reserved=reserve, reason=reason,
                detail=detail))
            return
        if equal_weight:
            # 回测 manual_buy 口径：min(base, 可用现金/n_left)，卖出回款
            # 视同当日已释放（回测中先卖后买），实盘以依赖单约束
            desired = min(desired, (cash_limit + pool.total) / n_left)
        elif buy_weights is not None:
            desired = min(desired, cash_limit + pool.total)
        qty = _lot_floor(desired, price)
        alloc: dict = {}
        while qty >= LOT_SIZE:
            turnover = qty * price
            fees = _est_fees(costs_fn, SIDE_BUY, turnover)
            need = turnover + fees
            if need <= cash_limit + 1e-9:
                break
            shortfall = need - cash_limit
            alloc = pool.allocate(shortfall)
            if sum(alloc.values()) >= shortfall - 1e-9:
                break
            qty -= LOT_SIZE
        turnover = qty * price
        if qty < LOT_SIZE or turnover < config.min_trade_amount:
            skipped.append({"symbol": symbol, "reason": (
                f"买入金额不足（可落实金额 {desired:.2f}，参考价 {price}，"
                f"min_trade_amount={config.min_trade_amount:.2f}），跳过")})
            pool.release(alloc)
            return
        fees = _est_fees(costs_fn, SIDE_BUY, turnover)
        need = turnover + fees
        if need <= cash_limit:
            reserve = need
            alloc = {}
        else:
            alloc = pool.allocate(need - cash_limit)
            if sum(alloc.values()) < need - cash_limit - 1e-9:
                skipped.append({"symbol": symbol, "reason": (
                    f"可用现金与卖出回款均不足（需 {need:.2f}，现金 "
                    f"{cash_limit:.2f}，可依赖回款 {pool.total:.2f}），跳过")})
                pool.release(alloc)
                return
            reserve = cash_limit
        remaining[0] -= reserve
        pool.commit(alloc)
        seq += 1
        orders.append(PlannedOrder(
            seq=seq, kind=kind, symbol=symbol, side=SIDE_BUY,
            planned_qty=qty, ref_price=price, est_value=turnover,
            est_fees=fees, budget_reserved=reserve, proceeds_alloc=dict(alloc),
            reason=reason, detail=detail))

    remaining = [max(spendable - config.cash_buffer, 0.0)] \
        if spendable is not None else [0.0]
    if spendable is None:
        for symbol, _, reason, detail, _eq in buy_requests:
            skipped.append({"symbol": symbol, "reason": (
                f"预算被阻止（{budget.blocked_reason}），买入跳过")})
        for order_ in buy_conditions:
            skipped.append({"symbol": order_["symbol"], "reason": (
                f"预算被阻止（{budget.blocked_reason}），条件买入跳过")})
    else:
        for index, (symbol, desired, reason, detail, equal_weight) in \
                enumerate(buy_requests):
            n_left = len(buy_requests) - index
            if symbol not in account.ref_prices:
                skipped.append({"symbol": symbol,
                                "reason": "买入缺参考价（无行情），阻止相关决策"})
                continue
            emit_buy(symbol, desired, reason, detail, equal_weight, n_left,
                     KIND_BUY, None)

        # 条件买入（LIMIT_BUY / BREAKOUT_BUY：触发价即参考价）
        for order_ in buy_conditions:
            symbol = order_["symbol"]
            ctype = order_.get("type", "")
            if ctype not in TRANSLATABLE_ENTRY_CONDITIONS:
                raise UnsupportedConditionError(
                    f"不支持的条件买入类型 {ctype!r}（已支持 "
                    f"{sorted(TRANSLATABLE_ENTRY_CONDITIONS)}），完整参数: {order_!r}")
            price = require_positive_price(order_["price"], f"{ctype}.price")
            if order_.get("shares") is not None:
                desired = int(order_["shares"]) * price
            else:
                desired = float(order_["value"])
            if account.holding(symbol) is not None and \
                    account.holding(symbol).policy == POLICY_OBSERVE:
                warnings.append(f"{symbol} 为 observe 持仓，条件买入被丢弃")
                continue
            emit_buy(symbol, desired, ctype,
                     {"type": ctype, "price": price,
                      "value": order_.get("value"), "shares": order_.get("shares")},
                     False, 1, KIND_ENTRY_COND, price,
                     fixed_qty=(int(order_["shares"])
                                if order_.get("shares") is not None else None),
                     contingent=True)

    reserved_total = sum(o.budget_reserved for o in orders)
    if spendable is not None and reserved_total > max(
            spendable - config.cash_buffer, 0.0) + 1e-6:
        raise PlanStateError(
            f"预算不变量被违背: 预留 {reserved_total:.2f} 超过可用 "
            f"{spendable - config.cash_buffer:.2f}——请报告此内部错误")

    # ── 持仓条件转换（§5.3：完整映射或明确拒绝）──
    for symbol in sorted(holding_conditions):
        conds = holding_conditions.get(symbol) or []
        if not conds:
            continue
        holding = account.holding(symbol)
        if holding is None:
            warnings.append(f"{symbol} 有条件单但已不在投影持仓中，条件丢弃")
            continue
        if holding.policy == POLICY_OBSERVE:
            warnings.append(
                f"{symbol} 为 observe 持仓，策略退出条件被丢弃（退出请人工处理）")
            continue
        planned_sell = sell_qty_by_symbol.get(symbol, 0)
        remaining_shares = holding.shares - planned_sell
        protect_count = 0
        for cond in conds:
            ctype = cond.get("type", "")
            if ctype not in TRANSLATABLE_EXIT_CONDITIONS:
                raise UnsupportedConditionError(
                    f"不支持的退出条件类型 {ctype!r}（已支持 "
                    f"{sorted(TRANSLATABLE_EXIT_CONDITIONS)}），完整参数: {cond!r}")
            if remaining_shares <= 0:
                warnings.append(
                    f"{symbol} 已有整仓卖出/退出计划，后续条件单 {ctype} 撤销"
                    "（避免同票重复卖出）")
                continue
            qty = remaining_shares
            order_warnings = []
            if planned_sell > 0:
                order_warnings.append(
                    f"同日已计划卖出 {planned_sell} 股，本单缩减至 {qty} 股")
            price = account.ref_prices.get(symbol)
            if ctype == "ML_EXIT":
                # ConditionBuilder 只在分数超阈值时生成 ML_EXIT——已满足的
                # 非价格退出转换为明确的次日卖出指令（§5.3）
                seq += 1
                ref = price if (price is not None and math.isfinite(price)
                                and price > 0) else 0.0
                orders.append(PlannedOrder(
                    seq=seq, kind=KIND_SELL, symbol=symbol, side=SIDE_SELL,
                    planned_qty=qty, ref_price=ref, est_value=qty * ref,
                    est_fees=_est_fees(costs_fn, SIDE_SELL, qty * ref),
                    reason="ML_EXIT",
                    detail={"model": cond.get("model"), "score": cond.get("score")},
                    warnings=order_warnings))
                sell_qty_by_symbol[symbol] = holding.shares
                remaining_shares = 0
                continue
            trigger = require_positive_price(cond["price"], f"{ctype}.price")
            protect_count += 1
            seq += 1
            orders.append(PlannedOrder(
                seq=seq, kind=KIND_PROTECT, symbol=symbol, side=SIDE_SELL,
                planned_qty=qty, ref_price=price or trigger,
                est_value=qty * trigger, est_fees=0.0,
                reason=ctype,
                detail={"type": ctype, "trigger_price": trigger,
                        "exclusive_group": symbol},
                warnings=order_warnings))
        if protect_count > 1:
            # 与回测条件单同序执行语义对齐：触发其一后立即撤同组其余，
            # 不输出可重复卖全仓的执行方案（A11）
            warnings.append(
                f"{symbol} 有 {protect_count} 个保护单（互斥组 {symbol}）："
                "与回测条件单同序监控，触发其一后立即撤销同组其余")

    return PlanDraft(orders=tuple(orders), skipped=tuple(skipped),
                     warnings=tuple(warnings), budget=budget, mode=mode)


# ── 持久化 ──

_PLAN_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS plan_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_plans (
    plan_id        VARCHAR PRIMARY KEY,
    revision       INTEGER NOT NULL DEFAULT 1,
    activation_id  INTEGER NOT NULL,
    snapshot_id    INTEGER NOT NULL,
    signal_date    VARCHAR NOT NULL,
    trade_date     VARCHAR NOT NULL,
    expiry_date    VARCHAR NOT NULL,
    status         VARCHAR NOT NULL,
    decision_hash  VARCHAR NOT NULL,
    decision_json  VARCHAR NOT NULL,
    skip_reason    VARCHAR NOT NULL DEFAULT '',
    cash_available_at_accept DOUBLE NOT NULL DEFAULT 0,
    reserved_total DOUBLE NOT NULL DEFAULT 0,
    created_at     VARCHAR NOT NULL,
    accepted_at    VARCHAR,
    superseded_by  VARCHAR,
    UNIQUE (activation_id, signal_date)
);

CREATE TABLE IF NOT EXISTS plan_orders (
    order_id          INTEGER PRIMARY KEY,
    plan_id           VARCHAR NOT NULL,
    client_order_id   VARCHAR NOT NULL UNIQUE,
    seq               INTEGER NOT NULL,
    kind              VARCHAR NOT NULL,
    symbol            VARCHAR NOT NULL,
    side              VARCHAR NOT NULL,
    reason            VARCHAR NOT NULL DEFAULT '',
    planned_qty       BIGINT NOT NULL,
    ref_price         DOUBLE NOT NULL,
    est_value         DOUBLE NOT NULL,
    est_fees          DOUBLE NOT NULL DEFAULT 0,
    budget_reserved   DOUBLE NOT NULL DEFAULT 0,
    proceeds_alloc_json VARCHAR NOT NULL DEFAULT '{}',
    detail_json       VARCHAR NOT NULL DEFAULT '{}',
    warnings_json     VARCHAR NOT NULL DEFAULT '[]',
    status            VARCHAR NOT NULL,
    filled_qty        BIGINT NOT NULL DEFAULT 0,
    avg_fill_price    DOUBLE,
    filled_fees       DOUBLE NOT NULL DEFAULT 0,
    created_at        VARCHAR NOT NULL,
    updated_at        VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_order_events (
    event_id    INTEGER PRIMARY KEY,
    order_id    INTEGER NOT NULL,
    event_at    VARCHAR NOT NULL,
    actor       VARCHAR NOT NULL DEFAULT 'user',
    event       VARCHAR NOT NULL,
    detail_json VARCHAR NOT NULL DEFAULT '{}'
);
"""

_ORDER_COLUMNS = (
    "order_id, plan_id, client_order_id, seq, kind, symbol, side, reason,"
    " planned_qty, ref_price, est_value, est_fees, budget_reserved,"
    " proceeds_alloc_json, detail_json, warnings_json, status, filled_qty,"
    " avg_fill_price, filled_fees, created_at, updated_at"
)


class PlanStore:
    """计划/订单表（plan_ 前缀）与生命周期状态机。

    全部写方法经 ``ledger.transaction()`` 组合事务；record_fill 与
    ledger.record_fill 原子完成（账户与订单原子更新，A19）。
    """

    def __init__(self, ledger: AccountLedger):
        self.ledger = ledger

    @classmethod
    def attach(cls, ledger: AccountLedger) -> "PlanStore":
        """幂等建表（可写连接）；schema 版本不匹配明确拒绝。"""
        store = cls(ledger)
        with ledger.transaction():
            ledger.connection.execute(_PLAN_SCHEMA_SQL)
            row = ledger.connection.execute(
                "SELECT value FROM plan_meta WHERE key = 'schema_version'"
            ).fetchone()
            version = row[0] if row else None
            if version is None:
                ledger.connection.execute(
                    "INSERT INTO plan_meta (key, value)"
                    " VALUES ('schema_version', ?)", (str(PLAN_SCHEMA_VERSION),))
            elif version != str(PLAN_SCHEMA_VERSION):
                raise PlanStateError(
                    f"计划表 schema_version={version} 不受支持"
                    f"（当前 {PLAN_SCHEMA_VERSION}）")
        return store

    # ── 查询 ──

    def plan(self, plan_id: str) -> Plan:
        row = self.ledger.connection.execute(
            "SELECT plan_id, revision, activation_id, snapshot_id, signal_date,"
            " trade_date, expiry_date, status, decision_hash, decision_json,"
            " skip_reason, cash_available_at_accept, reserved_total, created_at,"
            " accepted_at, superseded_by FROM plan_plans WHERE plan_id = ?",
            (plan_id,),
        ).fetchone()
        if row is None:
            raise PlanStateError(f"计划不存在: {plan_id}")
        return Plan(*row, orders=self.orders(plan_id=plan_id))

    def plans(self, status: str | None = None,
              activation_id: int | None = None) -> list[Plan]:
        sql = "SELECT plan_id FROM plan_plans"
        clauses, params = [], []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if activation_id is not None:
            clauses.append("activation_id = ?")
            params.append(activation_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY signal_date, plan_id"
        ids = [r[0] for r in
               self.ledger.connection.execute(sql, params).fetchall()]
        return [self.plan(pid) for pid in ids]

    def orders(self, plan_id: str | None = None,
               status: str | None = None) -> list[PlanOrder]:
        sql = f"SELECT {_ORDER_COLUMNS} FROM plan_orders"
        clauses, params = [], []
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY plan_id, seq"
        return [self._order_from_row(r) for r in
                self.ledger.connection.execute(sql, params).fetchall()]

    def order(self, client_order_id: str) -> PlanOrder:
        row = self.ledger.connection.execute(
            f"SELECT {_ORDER_COLUMNS} FROM plan_orders"
            " WHERE client_order_id = ?", (client_order_id,)).fetchone()
        if row is None:
            raise PlanStateError(f"订单不存在: {client_order_id}")
        return self._order_from_row(row)

    def _order_from_row(self, row) -> PlanOrder:
        return PlanOrder(
            order_id=row[0], plan_id=row[1], client_order_id=row[2], seq=row[3],
            kind=row[4], symbol=row[5], side=row[6], reason=row[7],
            planned_qty=row[8], ref_price=row[9], est_value=row[10],
            est_fees=row[11], budget_reserved=row[12],
            proceeds_alloc=json.loads(row[13]), detail=json.loads(row[14]),
            warnings=tuple(json.loads(row[15])), status=row[16],
            filled_qty=row[17], avg_fill_price=row[18], filled_fees=row[19],
            created_at=row[20], updated_at=row[21])

    def order_events(self, limit: int = 500) -> list[dict]:
        rows = self.ledger.connection.execute(
            "SELECT e.event_id, e.order_id, o.client_order_id, e.event_at, e.actor,"
            " e.event, e.detail_json FROM plan_order_events e"
            " JOIN plan_orders o ON o.order_id = e.order_id"
            " ORDER BY e.event_id DESC LIMIT ?", (int(limit),),
        ).fetchall()
        return [{"event_id": r[0], "order_id": r[1], "client_order_id": r[2],
                 "at": r[3], "actor": r[4], "event": r[5],
                 "detail": json.loads(r[6])} for r in rows]

    def outstanding_reserved(self) -> float:
        """未终结订单（planned/submitted/partial/unknown）占用的现金预留。"""
        placeholders = ", ".join("?" for _ in _ORDER_OPEN)
        row = self.ledger.connection.execute(
            f"SELECT COALESCE(SUM(budget_reserved), 0) FROM plan_orders"
            f" WHERE status IN ({placeholders})", tuple(sorted(_ORDER_OPEN)),
        ).fetchone()
        return float(row[0])

    def budget_context(self) -> BudgetContext:
        """当前预算上下文：待确认差异或负可用现金阻止新买入（§5.2）。

        takeover_gap 只影响收益视图（PnL incomplete），不阻止已确认接管
        后的交易；pending 候选与负 cash_available 才是阻止项。
        """
        projection = self.ledger.projection()
        pending = self.ledger.pending_candidates()
        blocked = None
        if pending:
            blocked = (f"存在 {len(pending)} 笔待确认快照差异"
                       f"（snapshot_id={pending[-1].snapshot_id}），先对账或接管")
        elif projection.cash_available < -1e-9:
            blocked = (f"投影可用现金为负 {projection.cash_available:.2f}"
                       "（事实不完整，缺入金/费用记录），不可作为交易依据")
        return BudgetContext(
            cash_available=projection.cash_available,
            outstanding_reserved=self.outstanding_reserved(),
            blocked_reason=blocked)

    # ── 生命周期：接受 / 跳过 / 过期 / 取消 ──

    def accept_plan(self, draft: PlanDraft, *, activation_id: int,
                    snapshot_id: int, signal_date: str, trade_date: str,
                    expiry_date: str, decision_hash: str,
                    actor: str = "user") -> Plan:
        """接受计划草稿：幂等 + 版本检查 + 预算预留 + 显式 supersede。

        - 幂等：同一 (activation, signal_date) 同内容重复接受返回已有计划；
          不同内容冲突拒绝（不制造重复有效计划或重复预留）。
        - 版本检查：确认快照必须是当前确认快照；旧快照版本的预览无法
          接受，要求重算。
        - supersede：此前 active/partial 计划标记 superseded；其 planned
          订单过期释放，submitted/partial/unknown 在途订单保留（不能
          当作从未执行）。
        """
        if not isinstance(draft, PlanDraft):
            raise PlanValidationError("accept_plan 需要 PlanDraft")
        activation_id = require_whole_int(activation_id, "activation_id", minimum=1)
        snapshot_id = require_whole_int(snapshot_id, "snapshot_id", minimum=1)
        signal_date = validate_date_format(signal_date, "signal_date")
        trade_date = validate_date_format(trade_date, "trade_date")
        expiry_date = validate_date_format(expiry_date, "expiry_date")
        decision_hash = require_text(decision_hash, "decision_hash")
        if trade_date < signal_date:
            raise PlanValidationError(
                f"trade_date {trade_date} 早于 signal_date {signal_date}")
        plan_id = "P" + _sha1(
            f"{activation_id}|{signal_date}|{decision_hash}|{trade_date}")[:16]
        with self.ledger.transaction():
            confirmed = self.ledger.confirmed_snapshot()
            if confirmed is None or confirmed.snapshot_id != snapshot_id:
                raise PlanStateError(
                    f"账户版本变化：预览基于 snapshot_id={snapshot_id}，当前确认"
                    f"基准为 {confirmed.snapshot_id if confirmed else '无'}"
                    "——请重新预览后再接受")
            existing = self.ledger.connection.execute(
                "SELECT plan_id, decision_hash, trade_date, status,"
                " snapshot_id FROM plan_plans"
                " WHERE activation_id = ? AND signal_date = ?",
                (activation_id, signal_date),
            ).fetchone()
            if existing is not None:
                if existing[3] == PLAN_SKIPPED:
                    raise PlanConflictError(
                        f"决策日 {signal_date} 已记录跳过——同日不能改为接受；"
                        "如需执行请在下一决策日重新预览")
                if existing[3] in (PLAN_ACTIVE, PLAN_PARTIAL, PLAN_COMPLETED):
                    if existing[4] == snapshot_id and (
                            existing[1] != decision_hash
                            or existing[2] != trade_date):
                        raise PlanConflictError(
                            f"决策日 {signal_date} 已有生效计划 {existing[0]}"
                            "（同快照下内容不同——预览可能已被该计划自身的"
                            "预留改变）。重复接受请直接沿用该计划；如需替换"
                            "先 cancel-plan")
                    # 快照已推进（预览预算可能被本计划自身预留改变）：
                    # 同日决策已生效，重复接受返回既有计划，不双倍预留
                    return self.plan(existing[0])
                if existing[3] in (PLAN_CANCELLED, PLAN_EXPIRED, PLAN_SUPERSEDED):
                    # UNIQUE(activation_id, signal_date) 使同日重建必然撞键；
                    # 给稳定 state 错误而非裸 ConstraintException（LIVE-V2-03）
                    raise PlanStateError(
                        f"决策日 {signal_date} 的计划已 {existing[3]}"
                        f"（{existing[0]}）——同日不能重建；"
                        "请在下一决策日重新预览")
                raise PlanConflictError(
                    f"决策日 {signal_date} 已存在记录（status={existing[3]}）"
                    "且内容不同——同日不接受不同决策")
            budget = self.budget_context()
            reserved_total = sum(o.budget_reserved for o in draft.orders)
            if budget.spendable is not None and reserved_total > max(
                    budget.spendable, 0.0) + 1e-6:
                raise PlanStateError(
                    f"接受时预算不再满足：需预留 {reserved_total:.2f}，"
                    f"当前可用 {budget.spendable:.2f}——请重新预览")
            superseded_in_flight: list[str] = []
            now = _now()
            prior_rows = self.ledger.connection.execute(
                "SELECT plan_id FROM plan_plans WHERE status IN (?, ?)",
                (PLAN_ACTIVE, PLAN_PARTIAL)).fetchall()
            for (prior_id,) in prior_rows:
                self.ledger.connection.execute(
                    "UPDATE plan_plans SET status = ?, superseded_by = ?"
                    " WHERE plan_id = ?", (PLAN_SUPERSEDED, plan_id, prior_id))
                for prior_order in self.orders(plan_id=prior_id):
                    if prior_order.status == ORDER_PLANNED:
                        self._transition_order(
                            prior_order, ORDER_EXPIRED, now, actor,
                            {"reason": "plan_superseded",
                             "released": prior_order.budget_reserved})
                    elif prior_order.status in (ORDER_SUBMITTED, ORDER_PARTIAL,
                                                ORDER_UNKNOWN):
                        superseded_in_flight.append(prior_order.client_order_id)
            decision_payload = {
                "orders": [o.canonical() for o in draft.orders],
                "skipped": [dict(s) for s in draft.skipped],
                "warnings": list(draft.warnings),
                "mode": draft.mode,
                "budget": {
                    "cash_available": _round6(budget.cash_available),
                    "outstanding_reserved": _round6(budget.outstanding_reserved),
                    "blocked_reason": budget.blocked_reason,
                },
            }
            self.ledger.connection.execute(
                "INSERT INTO plan_plans (plan_id, revision, activation_id,"
                " snapshot_id, signal_date, trade_date, expiry_date, status,"
                " decision_hash, decision_json, cash_available_at_accept,"
                " reserved_total, created_at, accepted_at)"
                " VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (plan_id, activation_id, snapshot_id, signal_date, trade_date,
                 expiry_date, PLAN_ACTIVE, decision_hash,
                 _canonical_json(decision_payload), budget.cash_available,
                 reserved_total, now, now))
            for order in draft.orders:
                self._insert_order(plan_id, order, now, actor)
            if superseded_in_flight:
                logger.warning(
                    "计划 %s supersede 前序计划时保留在途订单（不能当作从未"
                    "执行，需查询核验后终结）: %s", plan_id, superseded_in_flight)
            return self.plan(plan_id)

    def skip_decision(self, *, activation_id: int, snapshot_id: int,
                      signal_date: str, decision_hash: str, reason: str,
                      trade_date: str = "", actor: str = "user") -> Plan:
        """跳过（原因必填）：只记审计行，不生成订单、不占用预算。"""
        activation_id = require_whole_int(activation_id, "activation_id", minimum=1)
        snapshot_id = require_whole_int(snapshot_id, "snapshot_id", minimum=1)
        signal_date = validate_date_format(signal_date, "signal_date")
        decision_hash = require_text(decision_hash, "decision_hash")
        reason = require_text(reason, "reason")
        if trade_date:
            trade_date = validate_date_format(trade_date, "trade_date")
        plan_id = "S" + _sha1(
            f"{activation_id}|{signal_date}|{decision_hash}|skip")[:16]
        with self.ledger.transaction():
            existing = self.ledger.connection.execute(
                "SELECT plan_id, skip_reason FROM plan_plans WHERE plan_id = ?",
                (plan_id,)).fetchone()
            if existing is not None:
                if existing[1] != reason:
                    raise PlanConflictError(
                        f"决策日 {signal_date} 已有不同原因的跳过记录")
                return self.plan(plan_id)
            same_day = self.ledger.connection.execute(
                "SELECT plan_id, status FROM plan_plans"
                " WHERE activation_id = ? AND signal_date = ?",
                (activation_id, signal_date)).fetchone()
            if same_day is not None:
                raise PlanConflictError(
                    f"决策日 {signal_date} 已有记录（{same_day[1]}），不能再记跳过")
            now = _now()
            self.ledger.connection.execute(
                "INSERT INTO plan_plans (plan_id, activation_id, snapshot_id,"
                " signal_date, trade_date, expiry_date, status, decision_hash,"
                " decision_json, skip_reason, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)",
                (plan_id, activation_id, snapshot_id, signal_date, trade_date,
                 trade_date or signal_date, PLAN_SKIPPED, decision_hash,
                 reason, now))
            return self.plan(plan_id)

    def expire_plans(self, as_of: str, *, actor: str = "user") -> list[Plan]:
        """过期未执行的 active/partial 计划：expiry/trade 日已过即过期。

        planned 订单过期并释放预留；submitted/partial/unknown 在途订单
        保留（需查询核验后经执行回报终结），计划因此记 partial。
        """
        as_of = validate_date_format(as_of, "as_of")
        expired: list[Plan] = []
        with self.ledger.transaction():
            rows = self.ledger.connection.execute(
                "SELECT plan_id FROM plan_plans WHERE status IN (?, ?)",
                (PLAN_ACTIVE, PLAN_PARTIAL)).fetchall()
            for (plan_id,) in rows:
                plan = self.plan(plan_id)
                if plan.expiry_date >= as_of and plan.trade_date >= as_of:
                    continue
                now = _now()
                for order in plan.orders:
                    if order.status == ORDER_PLANNED:
                        self._transition_order(
                            order, ORDER_EXPIRED, now, actor,
                            {"reason": "plan_expired",
                             "released": order.budget_reserved})
                still_open = [o for o in self.orders(plan_id=plan_id)
                              if o.status in _ORDER_OPEN]
                executed = any(o.filled_qty for o in plan.orders)
                new_status = PLAN_PARTIAL if (still_open or executed) \
                    else PLAN_EXPIRED
                self.ledger.connection.execute(
                    "UPDATE plan_plans SET status = ? WHERE plan_id = ?",
                    (new_status, plan_id))
                expired.append(self.plan(plan_id))
        return expired

    def cancel_plan(self, plan_id: str, *, reason: str,
                    actor: str = "user") -> Plan:
        """取消计划：仅当无在途订单（submitted/partial/unknown）时允许。"""
        reason = require_text(reason, "reason")
        with self.ledger.transaction():
            plan = self.plan(plan_id)
            if plan.status not in (PLAN_ACTIVE, PLAN_PARTIAL):
                raise PlanStateError(
                    f"计划 {plan_id} 状态为 {plan.status}，不能取消")
            in_flight = [o.client_order_id for o in plan.open_orders
                         if o.status != ORDER_PLANNED]
            if in_flight:
                raise PlanStateError(
                    f"计划 {plan_id} 存在在途订单 {in_flight}——先核验执行结果"
                    "（record_fill / resolve_unknown / record_cancellation）再取消")
            now = _now()
            for order in plan.orders:
                if order.status == ORDER_PLANNED:
                    self._transition_order(
                        order, ORDER_CANCELLED, now, actor,
                        {"reason": reason, "released": order.budget_reserved})
            executed = any(o.filled_qty for o in plan.orders)
            new_status = PLAN_PARTIAL if executed else PLAN_CANCELLED
            self.ledger.connection.execute(
                "UPDATE plan_plans SET status = ? WHERE plan_id = ?",
                (new_status, plan_id))
            return self.plan(plan_id)

    # ── 执行状态机 ──

    def record_submission(self, client_order_id: str, *, actor: str = "user",
                          note: str = "") -> PlanOrder:
        """planned → submitted（幂等）；unknown 不能盲目重发，须经查询核验。"""
        with self.ledger.transaction():
            order = self.order(client_order_id)
            if order.status == ORDER_SUBMITTED:
                self._event(order, "resubmit_noop", _now(), actor,
                            {"note": note} if note else {})
                return self.order(client_order_id)
            if order.status != ORDER_PLANNED:
                raise PlanStateError(
                    f"订单 {client_order_id} 状态为 {order.status}，不能记录提交"
                    "（unknown 须先 resolve_unknown 查询核验）")
            self._transition_order(order, ORDER_SUBMITTED, _now(), actor,
                                   {"note": note} if note else {})
            return self.order(client_order_id)

    def record_fill(self, client_order_id: str, *, date, quantity, price,
                    fee_commission: float = 0.0, fee_tax: float = 0.0,
                    fee_transfer: float = 0.0, fee_other: float = 0.0,
                    source: str, trade_id: str, note: str = "",
                    actor: str = "user",
                    execution_config: ExecutionConfig | None = None) -> PlanOrder:
        """执行回报入账：订单更新 + ledger.record_fill 同一组合事务。

        计划接受绝不提前记成交；只有实际执行回报在此入账。超计划成交
        拒绝并保留可诊断输入（整笔回滚，无半订单/半成交）。
        """
        quantity = require_whole_int(quantity, "quantity", minimum=1)
        price = require_positive_price(price, "price")
        for name, value in (("fee_commission", fee_commission),
                            ("fee_tax", fee_tax),
                            ("fee_transfer", fee_transfer),
                            ("fee_other", fee_other)):
            require_non_negative(value, name)
        with self.ledger.transaction():
            order = self.order(client_order_id)
            plan = self.plan(order.plan_id)
            date = validate_date_format(date, "date")
            if date < plan.trade_date:
                raise PlanValidationError(
                    f"成交日期 {date} 早于计划执行日 {plan.trade_date}"
                    f"（{client_order_id}）——请核对回报归属")
            duplicate = self.ledger.find_fill(source, trade_id)
            if duplicate is not None:
                same_report = (
                    duplicate.order_ref == client_order_id
                    and duplicate.symbol == order.symbol
                    and duplicate.side == order.side
                    and duplicate.quantity == quantity
                    and abs(duplicate.price - price) <= 1e-9
                    and abs(duplicate.fee_commission - fee_commission) <= 1e-9
                    and abs(duplicate.fee_tax - fee_tax) <= 1e-9
                    and abs(duplicate.fee_transfer - fee_transfer) <= 1e-9
                    and abs(duplicate.fee_other - fee_other) <= 1e-9)
                if same_report:
                    # 重复回报幂等 no-op（先于终态守卫：补发已完成订单的
                    # 同身份回报不改订单/预留/账本，LIVE-V2-02）；异内容
                    # 落入下方 record_fill 抛身份冲突
                    return self.order(client_order_id)
            if plan.status not in (PLAN_ACTIVE, PLAN_PARTIAL, PLAN_SUPERSEDED):
                raise PlanStateError(
                    f"计划 {order.plan_id} 状态为 {plan.status}，不能再记成交")
            if order.status not in (ORDER_SUBMITTED, ORDER_PARTIAL, ORDER_UNKNOWN):
                raise PlanStateError(
                    f"订单 {client_order_id} 状态为 {order.status}——成交只能在"
                    "提交后回报；请先 record_submission")
            if (execution_config is not None
                    and execution_config.price_deviation_limit is not None
                    and order.ref_price > 0):
                deviation = abs(price - order.ref_price) / order.ref_price
                if deviation > execution_config.price_deviation_limit:
                    raise PlanValidationError(
                        f"成交价 {price} 偏离参考价 {order.ref_price} 超过 "
                        f"{execution_config.price_deviation_limit:.2%}"
                        f"（{client_order_id}）——请核对回报或调整参考价口径")
            new_filled = order.filled_qty + quantity
            if new_filled > order.planned_qty:
                raise PlanValidationError(
                    f"超计划成交: {client_order_id} 计划 {order.planned_qty} 股，"
                    f"已成交 {order.filled_qty}，本次再报 {quantity}"
                    "——整笔回滚，请核对回报拆分")
            # 账户事实与订单更新原子：ledger.record_fill 失败则整体回滚
            self.ledger.record_fill(
                date=date, symbol=order.symbol, side=order.side,
                quantity=quantity, price=price,
                fee_commission=fee_commission, fee_tax=fee_tax,
                fee_transfer=fee_transfer, fee_other=fee_other,
                source=source, trade_id=trade_id, plan_id=order.plan_id,
                order_ref=client_order_id, external=False, note=note)
            if order.side == SIDE_BUY and order.kind in (KIND_BUY,
                                                         KIND_ENTRY_COND):
                # 计划内建仓登记 managed 与真实入场锚点：确认快照推进后
                # 策略自己的持仓不能退化为 observe 不可见（A15 接管一致性）
                self.ledger.set_holding_policy(
                    order.symbol, POLICY_MANAGED,
                    reason=f"plan entry {order.plan_id}",
                    anchor_date=date, anchor_price=price)
            fees = fee_commission + fee_tax + fee_transfer + fee_other
            avg_price = ((order.avg_fill_price or 0.0) * order.filled_qty
                         + price * quantity) / new_filled
            status = ORDER_FILLED if new_filled == order.planned_qty \
                else ORDER_PARTIAL
            release = order.budget_reserved if status == ORDER_FILLED else 0.0
            now = _now()
            self.ledger.connection.execute(
                "UPDATE plan_orders SET status = ?, filled_qty = ?,"
                " avg_fill_price = ?, filled_fees = filled_fees + ?,"
                " budget_reserved = budget_reserved - ?, updated_at = ?"
                " WHERE order_id = ?",
                (status, new_filled, avg_price, fees, release, now,
                 order.order_id))
            self._event(order, "fill_recorded", now, actor, {
                "date": date, "quantity": quantity, "price": price,
                "fees": fees, "status": status, "released": release})
            self._refresh_plan_status(order.plan_id)
            return self.order(client_order_id)

    def record_rejection(self, client_order_id: str, *, reason: str,
                         actor: str = "user") -> PlanOrder:
        """提交被拒（planned/submitted/unknown → rejected），释放预留。"""
        reason = require_text(reason, "reason")
        with self.ledger.transaction():
            order = self.order(client_order_id)
            if order.status not in (ORDER_PLANNED, ORDER_SUBMITTED,
                                    ORDER_UNKNOWN):
                raise PlanStateError(
                    f"订单 {client_order_id} 状态为 {order.status}，不能记拒单")
            now = _now()
            self._transition_order(order, ORDER_REJECTED, now, actor,
                                   {"reason": reason,
                                    "released": order.budget_reserved})
            self._refresh_plan_status(order.plan_id)
            return self.order(client_order_id)

    def record_cancellation(self, client_order_id: str, *, reason: str,
                            actor: str = "user") -> PlanOrder:
        """撤单（submitted/partial/unknown → cancelled）：剩余未成交作废。"""
        reason = require_text(reason, "reason")
        with self.ledger.transaction():
            order = self.order(client_order_id)
            if order.status not in (ORDER_SUBMITTED, ORDER_PARTIAL,
                                    ORDER_UNKNOWN):
                raise PlanStateError(
                    f"订单 {client_order_id} 状态为 {order.status}，不能撤单")
            now = _now()
            self._transition_order(order, ORDER_CANCELLED, now, actor, {
                "reason": reason, "released": order.budget_reserved,
                "filled_qty": order.filled_qty,
                "remaining": order.planned_qty - order.filled_qty})
            self._refresh_plan_status(order.plan_id)
            return self.order(client_order_id)

    def mark_unknown(self, client_order_id: str, *, note: str = "",
                     actor: str = "user") -> PlanOrder:
        """提交超时/失联 → unknown：只能查询核验后恢复，不能盲目重发。"""
        with self.ledger.transaction():
            order = self.order(client_order_id)
            if order.status not in (ORDER_SUBMITTED, ORDER_PARTIAL):
                raise PlanStateError(
                    f"订单 {client_order_id} 状态为 {order.status}，"
                    "不能标记 unknown")
            now = _now()
            self._transition_order(order, ORDER_UNKNOWN, now, actor,
                                   {"note": note} if note else {})
            return self.order(client_order_id)

    def resolve_unknown(self, client_order_id: str, status: str, *,
                        fills: list | None = None, reason: str = "",
                        actor: str = "user", source: str = "query",
                        execution_config: ExecutionConfig | None = None) -> PlanOrder:
        """查询核验 unknown：落在 submitted/partial/filled/cancelled/rejected。"""
        if status not in (ORDER_SUBMITTED, ORDER_PARTIAL, ORDER_FILLED,
                          ORDER_CANCELLED, ORDER_REJECTED):
            raise PlanValidationError(
                "unknown 核验结果必须是 submitted/partial/filled/cancelled/"
                f"rejected: {status!r}")
        fills = fills or []
        with self.ledger.transaction():
            order = self.order(client_order_id)
            if order.status != ORDER_UNKNOWN:
                raise PlanStateError(
                    f"订单 {client_order_id} 状态为 {order.status}，不是 unknown")
            if status in (ORDER_FILLED, ORDER_PARTIAL):
                for i, fill in enumerate(fills, start=1):
                    self.record_fill(
                        client_order_id,
                        date=fill.get("date") or fill.get("trade_date"),
                        quantity=fill["quantity"], price=fill["price"],
                        fee_commission=fill.get("fee_commission", 0.0),
                        fee_tax=fill.get("fee_tax", 0.0),
                        fee_transfer=fill.get("fee_transfer", 0.0),
                        fee_other=fill.get("fee_other", 0.0),
                        source=source,
                        trade_id=require_text(
                            fill.get("trade_id") or f"{client_order_id}#q{i}",
                            "trade_id"),
                        note=fill.get("note", ""), actor=actor,
                        execution_config=execution_config)
                resolved = self.order(client_order_id)
                if status == ORDER_FILLED and resolved.status != ORDER_FILLED:
                    raise PlanStateError(
                        f"查询声称全部成交但回报股数不足（已回报 "
                        f"{resolved.filled_qty}/{resolved.planned_qty}）——"
                        "请补齐回报或改报 partial")
                return resolved
            if status == ORDER_SUBMITTED:
                self._transition_order(order, ORDER_SUBMITTED, _now(), actor,
                                       {"resolution": "still_working"})
                return self.order(client_order_id)
            now = _now()
            self._transition_order(order, status, now, actor, {
                "resolution": status,
                "reason": require_text(reason, "reason") if reason else "",
                "released": order.budget_reserved})
            self._refresh_plan_status(order.plan_id)
            return self.order(client_order_id)

    # ── 内部 ──

    def _insert_order(self, plan_id: str, order: PlannedOrder, now: str,
                      actor: str) -> None:
        order_id = self._next_order_id()
        client_order_id = f"{plan_id}#{order.seq:02d}"
        self.ledger.connection.execute(
            "INSERT INTO plan_orders (order_id, plan_id, client_order_id, seq,"
            " kind, symbol, side, reason, planned_qty, ref_price, est_value,"
            " est_fees, budget_reserved, proceeds_alloc_json, detail_json,"
            " warnings_json, status, filled_qty, avg_fill_price, filled_fees,"
            " created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL,"
            " 0, ?, ?)",
            (order_id, plan_id, client_order_id, order.seq, order.kind,
             order.symbol, order.side, order.reason, order.planned_qty,
             order.ref_price, order.est_value, order.est_fees,
             order.budget_reserved, _canonical_json(order.proceeds_alloc),
             _canonical_json(order.detail), _canonical_json(order.warnings),
             ORDER_PLANNED, now, now))
        self._event_by_id(order_id, "created", now, actor,
                          {"plan_id": plan_id, "kind": order.kind,
                           "symbol": order.symbol, "qty": order.planned_qty})

    def _next_order_id(self) -> int:
        row = self.ledger.connection.execute(
            "SELECT COALESCE(MAX(order_id), 0) + 1 FROM plan_orders").fetchone()
        return int(row[0])

    def _transition_order(self, order: PlanOrder, status: str, now: str,
                          actor: str, detail: dict) -> None:
        released = min(detail.get("released", 0.0) or 0.0, order.budget_reserved)
        self.ledger.connection.execute(
            "UPDATE plan_orders SET status = ?,"
            " budget_reserved = budget_reserved - ?, updated_at = ?"
            " WHERE order_id = ?",
            (status, released, now, order.order_id))
        self._event(order, f"status_{status}", now, actor, detail)

    def _event(self, order: PlanOrder, event: str, now: str, actor: str,
               detail: dict) -> None:
        self._event_by_id(order.order_id, event, now, actor, detail)

    def _event_by_id(self, order_id: int, event: str, now: str, actor: str,
                     detail: dict) -> None:
        row = self.ledger.connection.execute(
            "SELECT COALESCE(MAX(event_id), 0) + 1 FROM plan_order_events"
        ).fetchone()
        self.ledger.connection.execute(
            "INSERT INTO plan_order_events (event_id, order_id, event_at, actor,"
            " event, detail_json) VALUES (?, ?, ?, ?, ?, ?)",
            (int(row[0]), order_id, now, actor, event,
             _canonical_json(detail)))

    def _refresh_plan_status(self, plan_id: str) -> None:
        orders = self.orders(plan_id=plan_id)
        if any(o.status in _ORDER_OPEN for o in orders):
            if any(o.filled_qty for o in orders):
                self.ledger.connection.execute(
                    "UPDATE plan_plans SET status = ? WHERE plan_id = ?"
                    " AND status = ?", (PLAN_PARTIAL, plan_id, PLAN_ACTIVE))
            return
        new_status = PLAN_COMPLETED if any(o.filled_qty for o in orders) \
            else PLAN_CANCELLED
        self.ledger.connection.execute(
            "UPDATE plan_plans SET status = ? WHERE plan_id = ?"
            " AND status IN (?, ?)",
            (new_status, plan_id, PLAN_ACTIVE, PLAN_PARTIAL))


# ── 执行器（§5.3：手动 / 离线模拟共享接口） ──


class ManualExecutor:
    """手动执行器：不连接任何系统。

    submit() 仅确认"人工已按操作单提交"（订单进入 submitted）；成交/
    拒单/撤单由人工回报经 PlanStore.record_* 入账。instruction_sheet
    输出人类可读操作行（S3 CLI 排版）；instruction_sheet_from 服务预览草稿。
    """

    _ACTION_NAMES = {"BUY": "买入", "SELL": "卖出", "ENTRY_COND": "条件买入",
                     "PROTECT": "保护单"}

    def submit(self, order: PlanOrder, **_) -> ExecutionOutcome:
        return ExecutionOutcome(client_order_id=order.client_order_id,
                                status=ORDER_SUBMITTED)

    def query(self, order: PlanOrder, **_) -> ExecutionOutcome:
        raise PlanStateError(
            f"手动执行器不支持自动查询（{order.client_order_id}）——"
            "请人工核验后用 record_fill / resolve_unknown 回报")

    def _sheet_rows(self, orders) -> list[dict]:
        rows = []
        for order in orders:
            rows.append({
                "client_order_id": getattr(order, "client_order_id",
                                            f"#{order.seq:02d}"),
                "action": self._ACTION_NAMES[order.kind],
                "symbol": order.symbol,
                "quantity": order.planned_qty,
                "ref_price": order.ref_price,
                "trigger_price": order.detail.get("trigger_price"),
                "est_value": order.est_value,
                "est_fees": order.est_fees,
                "reason": order.reason,
                "depends_on": sorted(order.proceeds_alloc),
                "notes": list(order.warnings) + ["以券商可卖数量与实际价格为准"],
            })
        return rows

    def instruction_sheet(self, plan: Plan) -> list[dict]:
        return self._sheet_rows(
            o for o in plan.orders if o.status in _ORDER_OPEN)

    def instruction_sheet_from(self, draft) -> list[dict]:
        """预览草稿（PlanDraft.orders）→ 操作行，与 instruction_sheet 同构。"""
        return self._sheet_rows(draft.orders)


class SimulatedExecutor:
    """离线模拟执行器：拒单/部分成交/超时 unknown/重连查询测试用。

    script 按 client_order_id 指定 submit/query 行为；未指定的订单默认
    按参考价全额成交（零费用）。submit 值：
    - "fill_all"（默认）/ {"status": "filled"|"partial", "fills": [...]}
    - {"status": "rejected", "reason": ...}
    - {"status": "timeout"} → 提交超时（须 mark_unknown 后 query 核验）
    query 值：{"status": "filled"|"partial"|"cancelled"|"rejected"|"working",
    "fills": [...], "reason": ...}。
    """

    def __init__(self, script: dict | None = None):
        self.script = dict(script or {})

    def submit(self, order: PlanOrder, **_) -> ExecutionOutcome:
        spec = self.script.get(order.client_order_id, "fill_all")
        if isinstance(spec, str):
            spec = {"status": spec}
        status = {"fill_all": "filled", "timeout": ORDER_UNKNOWN}.get(
            spec.get("status", "filled"), spec.get("status", "filled"))
        if status == ORDER_UNKNOWN:
            return ExecutionOutcome(
                client_order_id=order.client_order_id, status=ORDER_UNKNOWN,
                reason="提交超时（模拟）")
        if status == ORDER_REJECTED:
            return ExecutionOutcome(
                client_order_id=order.client_order_id, status=ORDER_REJECTED,
                reason=spec.get("reason", "模拟拒单"))
        fills = tuple(
            FillReport(quantity=int(f["quantity"]),
                       price=float(f.get("price", order.ref_price)),
                       fee_commission=float(f.get("fee_commission", 0.0)),
                       fee_tax=float(f.get("fee_tax", 0.0)),
                       fee_transfer=float(f.get("fee_transfer", 0.0)),
                       fee_other=float(f.get("fee_other", 0.0)))
            for f in spec.get("fills", []) if int(f["quantity"]) > 0)
        if not fills:
            fills = (FillReport(quantity=order.planned_qty,
                                price=order.ref_price),)
        reported = sum(f.quantity for f in fills)
        final = ORDER_FILLED if reported >= order.planned_qty else ORDER_PARTIAL
        return ExecutionOutcome(client_order_id=order.client_order_id,
                                status=final, fills=fills)

    def query(self, order: PlanOrder, **_) -> ExecutionOutcome:
        spec = self.script.get(order.client_order_id, {}).get("query")
        if spec is None:
            spec = {"status": "filled", "fills": [
                {"quantity": order.planned_qty - order.filled_qty,
                 "price": order.ref_price}]}
        status = {"working": ORDER_SUBMITTED}.get(
            spec.get("status", "filled"), spec.get("status", "filled"))
        fills = tuple(
            FillReport(quantity=int(f["quantity"]),
                       price=float(f.get("price", order.ref_price)))
            for f in spec.get("fills", []))
        return ExecutionOutcome(client_order_id=order.client_order_id,
                                status=status, fills=fills,
                                reason=spec.get("reason", ""))


@dataclass(frozen=True)
class OrderGate:
    """依赖买单的可执行额度判定（A11：部分回款只释放相应预算）。"""

    client_order_id: str
    allowed: bool
    quantity: int
    reason: str = ""


def gate_order(store: "PlanStore", client_order_id: str) -> OrderGate:
    """卖出依赖未成交的买单不可执行；已确认回款按比例解锁。"""
    order = store.order(client_order_id)
    if order.status not in (ORDER_PLANNED, ORDER_SUBMITTED, ORDER_PARTIAL,
                            ORDER_UNKNOWN):
        return OrderGate(client_order_id, False, 0,
                         f"订单已终结（{order.status}）")
    remaining = order.planned_qty - order.filled_qty
    if remaining <= 0:
        return OrderGate(client_order_id, False, 0, "订单已全部成交")
    if order.side != SIDE_BUY or not order.proceeds_alloc:
        return OrderGate(client_order_id, True, remaining)
    plan_orders = {f"#{o.seq:02d}": o for o in store.orders(plan_id=order.plan_id)}
    released = 0.0
    unfilled = []
    no_fill = []
    for cid, amount in sorted(order.proceeds_alloc.items()):
        dep = plan_orders.get(cid)
        if dep is None:
            continue
        if dep.planned_qty > 0 and dep.filled_qty > 0:
            ratio = min(1.0, dep.filled_qty / dep.planned_qty)
            released += amount * ratio
        if dep.filled_qty <= 0:
            no_fill.append(cid)
        if dep.filled_qty < dep.planned_qty:
            unfilled.append(cid)
    if no_fill:
        # 依赖卖单完全未成交前整单等待：先执行卖出，回款后按比例解锁
        return OrderGate(
            client_order_id, False, 0,
            f"依赖卖单 {no_fill} 尚无成交回报——先执行卖出并回报成交，"
            "部分回款只释放相应预算")
    available_value = order.budget_reserved + released
    qty = int(available_value / order.ref_price / LOT_SIZE) * LOT_SIZE \
        if order.ref_price > 0 else 0
    qty = min(qty, remaining)
    if qty < LOT_SIZE:
        detail = f"；未成交依赖卖单 {unfilled}" if unfilled else ""
        return OrderGate(
            client_order_id, False, 0,
            f"依赖卖出回款不足一手（可解锁金额 {available_value:.2f}）——"
            f"先执行卖出并回报成交，部分回款只释放相应预算{detail}")
    return OrderGate(client_order_id, True, qty)


def apply_execution(store: PlanStore, outcome: ExecutionOutcome, *,
                    actor: str = "user", source: str = "simulator",
                    execution_config: ExecutionConfig | None = None,
                    trade_date: str = "") -> list[PlanOrder]:
    """把执行器输出落到 PlanStore（提交/超时/拒单/成交的状态编排）。

    每笔 outcome 独立事务：一单失败不回滚已入账的其他单（与逐笔手工
    回报同语义）；成交回报失败（超计划/价格偏离）原样上抛。
    """
    client_order_id = outcome.client_order_id
    order = store.order(client_order_id)
    results: list[PlanOrder] = []
    if outcome.status == ORDER_UNKNOWN:
        if order.status == ORDER_PLANNED:
            store.record_submission(client_order_id, actor=actor,
                                    note="timeout-after-submit")
        results.append(store.mark_unknown(
            client_order_id, note=outcome.reason, actor=actor))
        return results
    if outcome.status == ORDER_REJECTED:
        results.append(store.record_rejection(
            client_order_id, reason=outcome.reason or "执行器拒单", actor=actor))
        return results
    if order.status == ORDER_PLANNED:
        store.record_submission(client_order_id, actor=actor)
    order = store.order(client_order_id)
    fill_date = trade_date or store.plan(order.plan_id).trade_date
    for i, fill in enumerate(outcome.fills, start=1):
        results.append(store.record_fill(
            client_order_id, date=fill_date,
            quantity=fill.quantity, price=fill.price,
            fee_commission=fill.fee_commission, fee_tax=fill.fee_tax,
            fee_transfer=fill.fee_transfer, fee_other=fill.fee_other,
            source=source, trade_id=f"{client_order_id}#f{i}", actor=actor,
            execution_config=execution_config))
    if not outcome.fills:
        results.append(store.order(client_order_id))
    return results
