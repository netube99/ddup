"""实盘 V2 账户层 — 独立版本化 DuckDB 账本（docs/live_redesign.md §3）。

职责（账户事实与记账契约）：
- 快照与来源（§3.1）：完整券商快照的候选 / 自动确认 / 接管与差异；
  同来源身份同内容幂等，同身份异内容报冲突；缺 holdings 视为缺必需字段。
- 成交与修正（§3.2）：以 (source, trade_id) 稳定身份幂等入账；无券商 ID 的
  批量行用 batch+row 身份；更正走引用原记录的 correction/void 事件，不改历史。
- 现金流与权益（§3.3）：deposit/withdrawal/fee/dividend/tax/explicit_adjustment
  分类入账，绝不把不明差额自动当作入金；投资 PnL = 期末权益 - 期初权益
  - 净外部出入金，接管缺口或缺价时明确 incomplete。
- 持仓管理政策（§3.4）：managed / exit_only / observe；managed 必须显式
  提供入场锚点，锚点未知默认 observe，不编造日期。

边界：不依赖 Engine/策略/行情，不复用回测结果库 schema（无 init_backtest_db）；
估值价格由调用方传入，行情缺失不阻止保存账户事实。单进程串行写，
所有公共写方法整体事务化，失败不残留半行事实；拒绝的输入写拒绝审计
（acct_audit）供诊断，事实表不受污染。

S2 扩展契约：计划/订单表由 live_plan 在同一账本库中创建，统一经
``AccountLedger.open`` 取得 ``ledger.connection`` 并在 ``with ledger.transaction()``
内建表写入；``acct_`` 前缀表名为账户层保留。``ledger.transaction()`` 是可组合
事务：最外层是唯一 commit/rollback owner，内部写方法自动加入同一事务且
全部“先校验后写入”；任一嵌套作用域失败（含写前校验，即使异常被调用方
捕获）都会把组合事务标记 rollback-only，外层在真正 commit 前检测到即
整体回滚并抛 AccountStateError——绝不提交部分更新。账户公共类型与
异常在本模块一次定义，后续组件复用，不得反向 import 打破分层。

负投影现金仅是“事实不完整”的信号（缺入金/费用记录），账户层只告警不
拒绝，它绝不是可交易状态：预算/执行层必须阻止以负或不确定（存在待确认
差异）的可用资金发起新买入。
"""

import hashlib
import json
import logging
import math
import os
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import duckdb

from btcore.database import assert_duckdb_file
from btcore.database import transaction as db_transaction

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"
SIDES = frozenset({SIDE_BUY, SIDE_SELL})

SNAPSHOT_CANDIDATE = "candidate"
SNAPSHOT_CONFIRMED = "confirmed"
SNAPSHOT_EXPIRED = "expired"

CASH_DEPOSIT = "deposit"
CASH_WITHDRAWAL = "withdrawal"
CASH_FEE = "fee"
CASH_DIVIDEND = "dividend"
CASH_TAX = "tax"
CASH_EXPLICIT_ADJUSTMENT = "explicit_adjustment"
CASH_FLOW_CATEGORIES = frozenset({
    CASH_DEPOSIT, CASH_WITHDRAWAL, CASH_FEE, CASH_DIVIDEND, CASH_TAX,
    CASH_EXPLICIT_ADJUSTMENT,
})
# 分类符号约束：财务分类不因金额大小或容差改变（§3.3）
_CATEGORY_SIGN = {
    CASH_DEPOSIT: "positive",
    CASH_WITHDRAWAL: "negative",
    CASH_FEE: "negative",
    CASH_DIVIDEND: "positive",
    CASH_TAX: "negative",
    CASH_EXPLICIT_ADJUSTMENT: "nonzero",
}

POLICY_MANAGED = "managed"
POLICY_EXIT_ONLY = "exit_only"
POLICY_OBSERVE = "observe"
HOLDING_POLICIES = frozenset({POLICY_MANAGED, POLICY_EXIT_ONLY, POLICY_OBSERVE})

SHARE_ADJ_CORPORATE_ACTION = "corporate_action"
SHARE_ADJ_MANUAL = "manual"
SHARE_ADJ_CATEGORIES = frozenset({SHARE_ADJ_CORPORATE_ACTION, SHARE_ADJ_MANUAL})

CORRECTION_FIELDS = frozenset({
    "date", "price", "quantity", "fee_commission", "fee_tax", "fee_transfer",
    "fee_other",
})

CORRECTION = "correction"
VOID = "void"

# 现金对账容差（CNY）：仅在"是否构成差异"判定中使用，不改变财务分类
DEFAULT_CASH_TOLERANCE = 0.01

# 事实表清单：接管/过期清理与状态计数使用
_FACT_TABLES = ("acct_fills", "acct_cash_flows", "acct_share_adjustments")


# ── 异常：code 供 CLI 映射稳定错误码（§6）──


class LiveAccountError(Exception):
    """实盘账户层错误基类。"""

    code = "account_error"


class AccountValidationError(LiveAccountError):
    """输入非法（非有限值、非法日期、小数股数、缺必需字段等）。"""

    code = "validation"


class AccountConflictError(LiveAccountError):
    """同一稳定身份出现不同内容，拒绝静默覆盖。"""

    code = "conflict"


class LedgerSchemaError(LiveAccountError):
    """账本库身份/版本不受支持（旧 V1、未知版本、非账本文件）。"""

    code = "schema"


class LedgerPathError(LiveAccountError):
    """路径不存在或不允许悄悄创建。"""

    code = "not_found"


class AccountStateError(LiveAccountError):
    """状态矛盾：无确认基准、超卖、过期候选、只读写入等。"""

    code = "state"


# ── 输入校验（供 S2/S3 复用）──


def require_finite(value, name: str) -> float:
    """数值入口 fail-fast：拒绝非数值、NaN/Inf 与布尔伪数值。"""
    if isinstance(value, bool):
        raise AccountValidationError(f"{name} 非数值（布尔）: {value!r}")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise AccountValidationError(f"{name} 非法（非数值）: {value!r}") from None
    if not math.isfinite(number):
        raise AccountValidationError(f"{name} 非法（NaN/Inf）: {value!r}")
    return number


def validate_date_format(value, name: str = "date") -> str:
    """校验 YYYYMMDD 八位真实日历日期，返回规范字符串。"""
    text = str(value)
    if len(text) != 8 or not text.isdigit():
        raise AccountValidationError(f"{name} 需为 YYYYMMDD 八位数字: {value!r}")
    try:
        datetime.strptime(text, "%Y%m%d")
    except ValueError:
        raise AccountValidationError(f"{name} 非法日期: {value!r}") from None
    return text


def require_whole_int(value, name: str, minimum: int | None = None) -> int:
    """整数入口：拒绝布尔伪整数、小数与非数值；可带下限。"""
    if isinstance(value, bool):
        raise AccountValidationError(f"{name} 非法整数（布尔）: {value!r}")
    if isinstance(value, int):
        number = value
    else:
        try:
            number_f = float(value)
        except (TypeError, ValueError):
            raise AccountValidationError(f"{name} 非法整数: {value!r}") from None
        if not math.isfinite(number_f) or not number_f.is_integer():
            raise AccountValidationError(f"{name} 必须为整数: {value!r}")
        number = int(number_f)
    if minimum is not None and number < minimum:
        raise AccountValidationError(f"{name} 不能小于 {minimum}: {value!r}")
    return number


def require_positive_price(value, name: str) -> float:
    number = require_finite(value, name)
    if number <= 0:
        raise AccountValidationError(f"{name} 必须为正数: {value!r}")
    return number


def require_non_negative(value, name: str) -> float:
    number = require_finite(value, name)
    if number < 0:
        raise AccountValidationError(f"{name} 不能为负: {value!r}")
    return number


def require_text(value, name: str) -> str:
    if not isinstance(value, str):
        raise AccountValidationError(f"{name} 必须为非空字符串: {value!r}")
    text = value.strip()
    if not text:
        raise AccountValidationError(f"{name} 不能为空")
    return text


def optional_text(value, name: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str):
        raise AccountValidationError(f"{name} 必须为字符串: {value!r}")
    return value.strip()


def _round6(value: float) -> float:
    return round(float(value), 6)


def _same_content(stored: dict, incoming: dict) -> bool:
    """身份内容比对：浮点按 6 位小数归一，键以已存列为准。"""
    for key, other in stored.items():
        if key not in incoming:
            return False
        value = incoming[key]
        if isinstance(other, float) or isinstance(value, float):
            if _round6(other) != _round6(value):
                return False
        elif other != value:
            return False
    return True


def _content_conflict(kind: str, identity: tuple, stored: dict,
                      incoming: dict) -> AccountConflictError:
    return AccountConflictError(
        f"{kind}身份 {identity} 已存在且内容不同——已存 {stored}，本次 {incoming}；"
        "拒绝静默覆盖。幂等只认稳定身份+相同内容（不同 ID 的相同成交都保留）"
    )


def _now() -> str:
    return datetime.now().isoformat(timespec="microseconds")


def _content_hash(payload) -> str:
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ── 领域数据类（冻结供 S2 复用）──


@dataclass(frozen=True)
class SnapshotHolding:
    """快照持仓：股数、可卖数量与可选的账户成本（每股均价，CNY）。"""

    symbol: str
    shares: int
    available: int
    cost: float | None = None


@dataclass(frozen=True)
class AccountSnapshot:
    """持久化的完整券商快照（§3.1）。raw_json 保留规范化原始输入。"""

    snapshot_id: int
    status: str
    as_of: str
    captured_at: str
    source: str
    import_id: str
    cash_balance: float
    cash_available: float
    holdings: dict = field(default_factory=dict)  # symbol -> SnapshotHolding
    note: str = ""
    raw_json: str = ""
    content_hash: str = ""


@dataclass(frozen=True)
class CashDiff:
    expected: float  # 账本投影现金
    actual: float    # 券商快照现金
    delta: float


@dataclass(frozen=True)
class HoldingDiff:
    symbol: str
    expected: int
    actual: int
    delta: int


@dataclass(frozen=True)
class SnapshotDiff:
    """候选快照与账本投影的差异；tolerance 仅用于现金判定。"""

    cash: CashDiff | None
    holding_diffs: dict = field(default_factory=dict)  # symbol -> HoldingDiff
    tolerance: float = DEFAULT_CASH_TOLERANCE

    @property
    def clean(self) -> bool:
        return self.cash is None and not self.holding_diffs

    def summary(self) -> str:
        parts = []
        if self.cash is not None:
            parts.append(
                f"cash expected={self.cash.expected:.2f} actual={self.cash.actual:.2f}"
                f" delta={self.cash.delta:+.2f}"
            )
        for symbol in sorted(self.holding_diffs):
            d = self.holding_diffs[symbol]
            parts.append(f"{symbol} expected={d.expected} actual={d.actual}")
        return "; ".join(parts) if parts else "clean"


@dataclass(frozen=True)
class ImportSnapshotResult:
    snapshot: AccountSnapshot
    diff: SnapshotDiff | None
    confirmed: bool
    action: str  # opening_confirmed | imported_confirmed | stored_candidate | idempotent_noop


@dataclass(frozen=True)
class SnapshotReviewResult:
    """reconcile_candidate 的结果：是否确认与剩余差异。"""

    snapshot: AccountSnapshot
    diff: SnapshotDiff | None
    confirmed: bool
    action: str  # reconciled_confirmed | reconciled_pending


@dataclass(frozen=True)
class FillRecord:
    """真实成交（§3.2）。(source, trade_id) 是稳定身份。"""

    fill_id: int
    source: str
    trade_id: str
    date: str
    symbol: str
    side: str
    quantity: int
    price: float
    fee_commission: float = 0.0
    fee_tax: float = 0.0
    fee_transfer: float = 0.0
    fee_other: float = 0.0
    plan_id: str = ""
    order_ref: str = ""
    external: bool = False
    note: str = ""
    created_at: str = ""
    superseded_by: int | None = None


@dataclass(frozen=True)
class ImportFillsResult:
    appended: int
    skipped: int
    fills: tuple = ()  # 新入账的 FillRecord
    action: str = "imported"  # imported | idempotent_noop


@dataclass(frozen=True)
class FillCorrection:
    """引用原成交的修正事件；kind = correction | void，不改写历史行。"""

    correction_id: int
    fill_id: int
    target_trade_id: str
    kind: str
    date: str
    fields: dict
    source: str
    event_id: str
    note: str = ""
    created_at: str = ""


@dataclass(frozen=True)
class CashFlow:
    flow_id: int
    date: str
    category: str
    amount: float  # 带符号 CNY：入金为正、出金/费用/税费为负
    symbol: str = ""
    source: str = ""
    event_id: str = ""
    note: str = ""
    created_at: str = ""
    superseded_by: int | None = None


@dataclass(frozen=True)
class ShareAdjustment:
    """股数调整（送转/转增等到账）：只改投影股数，不伪造成交。"""

    adj_id: int
    date: str
    symbol: str
    delta_shares: int
    category: str
    reason: str = ""
    source: str = ""
    event_id: str = ""
    created_at: str = ""
    superseded_by: int | None = None


@dataclass(frozen=True)
class TakeoverRecord:
    """接管记录：差异前后值与原因留档，标记历史不完整。"""

    takeover_id: int
    snapshot_id: int
    as_of: str
    reason: str
    diff_json: str
    created_at: str = ""


@dataclass(frozen=True)
class AuditEvent:
    event_seq: int
    at: str
    actor: str
    action: str
    subject_type: str
    subject_id: str
    detail_json: str


@dataclass
class ProjectedHolding:
    """投影持仓：股数与可选成本金额（cost_known=False 表示基准缺成本）。"""

    symbol: str
    shares: int
    cost_amount: float = 0.0
    cost_known: bool = False

    @property
    def avg_cost(self) -> float | None:
        if not self.cost_known or self.shares <= 0:
            return None
        return self.cost_amount / self.shares


@dataclass(frozen=True)
class ProjectedState:
    """从最新确认基准投影的账户状态。

    cash_available 是估计值：基准可用现金 + 期间净现金变动；冻结信息只在
    快照时点由券商确认，不能凭空推断（§3.3 现金分别表达）。
    cash 为负仅表示事实不完整（缺入金/费用记录），不是可交易状态；
    预算/执行层必须自行阻止以负或不确定可用资金发起新买入。
    """

    base_snapshot_id: int
    base_as_of: str
    cash: float
    cash_available: float
    holdings: dict = field(default_factory=dict)  # symbol -> ProjectedHolding
    projected_through: str = ""
    pre_base_fact_count: int = 0


@dataclass(frozen=True)
class EquityView:
    cash: float
    holdings_value: float | None  # 缺价时为 None
    total: float | None
    missing_prices: tuple = ()


@dataclass(frozen=True)
class PnLView:
    """投资 PnL 视图：期末权益 - 期初权益 - 净外部出入金（§3.3）。

    complete=False 时 investment_pnl 为 None 且 reasons 说明缺口；
    不得对 incomplete 视图宣称精确收益率。
    """

    opening_as_of: str | None
    closing_as_of: str | None
    opening_equity: float | None
    closing_equity: float | None
    net_external_flows: float
    investment_pnl: float | None
    complete: bool
    reasons: tuple = ()


@dataclass(frozen=True)
class PendingCandidate:
    snapshot_id: int
    as_of: str
    source: str
    import_id: str
    diff: SnapshotDiff


@dataclass(frozen=True)
class HoldingPolicy:
    symbol: str
    policy: str
    reason: str
    expire_date: str | None
    anchor_date: str | None
    anchor_price: float | None
    updated_at: str


@dataclass(frozen=True)
class LedgerStatus:
    schema_version: int
    account_id: str
    broker: str
    created_at: str
    confirmed_snapshot: AccountSnapshot | None
    pending: tuple  # (PendingCandidate, ...)
    history_incomplete: bool
    gap_count: int
    counts: dict
    projected: ProjectedState | None


# ── schema（独立账本库，版本化；不复用回测 schema）──

_ACCOUNT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS acct_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS acct_snapshots (
    snapshot_id   INTEGER PRIMARY KEY,
    status        VARCHAR NOT NULL,
    as_of         VARCHAR NOT NULL,
    captured_at   VARCHAR NOT NULL,
    source        VARCHAR NOT NULL,
    import_id     VARCHAR NOT NULL,
    cash_balance  DOUBLE NOT NULL,
    cash_available DOUBLE NOT NULL,
    note          VARCHAR NOT NULL DEFAULT '',
    raw_json      VARCHAR NOT NULL DEFAULT '',
    content_hash  VARCHAR NOT NULL,
    UNIQUE (source, import_id)
);

CREATE TABLE IF NOT EXISTS acct_snapshot_holdings (
    snapshot_id INTEGER NOT NULL,
    symbol      VARCHAR NOT NULL,
    shares      BIGINT NOT NULL,
    available   BIGINT NOT NULL,
    cost        DOUBLE,
    PRIMARY KEY (snapshot_id, symbol)
);

CREATE TABLE IF NOT EXISTS acct_pending_diffs (
    snapshot_id INTEGER NOT NULL,
    kind        VARCHAR NOT NULL,
    symbol      VARCHAR NOT NULL DEFAULT '',
    expected    DOUBLE NOT NULL,
    actual      DOUBLE NOT NULL,
    PRIMARY KEY (snapshot_id, kind, symbol)
);

CREATE TABLE IF NOT EXISTS acct_import_batches (
    source       VARCHAR NOT NULL,
    batch_id     VARCHAR NOT NULL,
    content_hash VARCHAR NOT NULL,
    n_rows       INTEGER NOT NULL,
    created_at   VARCHAR NOT NULL,
    PRIMARY KEY (source, batch_id)
);

CREATE TABLE IF NOT EXISTS acct_fills (
    fill_id       INTEGER PRIMARY KEY,
    source        VARCHAR NOT NULL,
    trade_id      VARCHAR NOT NULL,
    date          VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    side          VARCHAR NOT NULL,
    quantity      BIGINT NOT NULL,
    price         DOUBLE NOT NULL,
    fee_commission DOUBLE NOT NULL DEFAULT 0,
    fee_tax       DOUBLE NOT NULL DEFAULT 0,
    fee_transfer  DOUBLE NOT NULL DEFAULT 0,
    fee_other     DOUBLE NOT NULL DEFAULT 0,
    plan_id       VARCHAR NOT NULL DEFAULT '',
    order_ref     VARCHAR NOT NULL DEFAULT '',
    external      BOOLEAN NOT NULL DEFAULT FALSE,
    note          VARCHAR NOT NULL DEFAULT '',
    created_at    VARCHAR NOT NULL,
    superseded_by INTEGER,
    UNIQUE (source, trade_id)
);

CREATE TABLE IF NOT EXISTS acct_fill_corrections (
    correction_id INTEGER PRIMARY KEY,
    fill_id       INTEGER NOT NULL,
    kind          VARCHAR NOT NULL,
    date          VARCHAR NOT NULL,
    fields_json   VARCHAR NOT NULL DEFAULT '{}',
    source        VARCHAR NOT NULL,
    event_id      VARCHAR NOT NULL,
    note          VARCHAR NOT NULL DEFAULT '',
    created_at    VARCHAR NOT NULL,
    UNIQUE (source, event_id)
);

CREATE TABLE IF NOT EXISTS acct_cash_flows (
    flow_id       INTEGER PRIMARY KEY,
    date          VARCHAR NOT NULL,
    category      VARCHAR NOT NULL,
    amount        DOUBLE NOT NULL,
    symbol        VARCHAR NOT NULL DEFAULT '',
    source        VARCHAR NOT NULL,
    event_id      VARCHAR NOT NULL,
    note          VARCHAR NOT NULL DEFAULT '',
    created_at    VARCHAR NOT NULL,
    superseded_by INTEGER,
    UNIQUE (source, event_id)
);

CREATE TABLE IF NOT EXISTS acct_share_adjustments (
    adj_id        INTEGER PRIMARY KEY,
    date          VARCHAR NOT NULL,
    symbol        VARCHAR NOT NULL,
    delta_shares  BIGINT NOT NULL,
    category      VARCHAR NOT NULL,
    reason        VARCHAR NOT NULL DEFAULT '',
    source        VARCHAR NOT NULL,
    event_id      VARCHAR NOT NULL,
    created_at    VARCHAR NOT NULL,
    superseded_by INTEGER,
    UNIQUE (source, event_id)
);

CREATE TABLE IF NOT EXISTS acct_takeovers (
    takeover_id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    as_of       VARCHAR NOT NULL,
    reason      VARCHAR NOT NULL,
    diff_json   VARCHAR NOT NULL,
    created_at  VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS acct_holding_policies (
    symbol      VARCHAR PRIMARY KEY,
    policy      VARCHAR NOT NULL,
    reason      VARCHAR NOT NULL DEFAULT '',
    expire_date VARCHAR,
    anchor_date VARCHAR,
    anchor_price DOUBLE,
    updated_at  VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS acct_audit (
    event_seq   INTEGER PRIMARY KEY,
    event_at    VARCHAR NOT NULL,
    actor       VARCHAR NOT NULL DEFAULT 'user',
    action      VARCHAR NOT NULL,
    subject_type VARCHAR NOT NULL DEFAULT '',
    subject_id  VARCHAR NOT NULL DEFAULT '',
    detail_json VARCHAR NOT NULL DEFAULT '{}'
);
"""

_FILL_COLUMNS = (
    "fill_id, source, trade_id, date, symbol, side, quantity, price,"
    " fee_commission, fee_tax, fee_transfer, fee_other, plan_id, order_ref,"
    " external, note, created_at, superseded_by"
)
_FILL_KEYS = (
    "fill_id", "source", "trade_id", "date", "symbol", "side", "quantity",
    "price", "fee_commission", "fee_tax", "fee_transfer", "fee_other",
    "plan_id", "order_ref", "external", "note", "created_at", "superseded_by",
)


class AccountLedger:
    """实盘 V2 账本库：单进程串行写，公共写方法整体事务化。

    打开方式：
    - ``AccountLedger.create(path, account_id=...)`` 建新库（拒绝覆盖已有文件）；
    - ``AccountLedger.open(path)`` 打开已有库并校验 schema 身份；
    - ``AccountLedger.open(path, read_only=True)`` 只读打开，绝不写入。
    """

    def __init__(self, conn: duckdb.DuckDBPyConnection, path: str,
                 *, read_only: bool):
        self._conn = conn
        self.path = path
        self.read_only = read_only
        self._txn_depth = 0
        self._txn_poisoned = False

    # ── 连接与事务 ──

    @property
    def connection(self) -> duckdb.DuckDBPyConnection:
        """底层连接；S2 在同一库建计划表时复用。"""
        return self._conn

    @property
    def account_id(self) -> str:
        return self._meta("account_id") or ""

    @property
    def broker(self) -> str:
        return self._meta("broker") or ""

    @contextmanager
    def transaction(self):
        """可组合事务边界（S2 使用）：最外层是唯一 commit/rollback owner。

        组合规则：
        - 首个进入者是 owner，负责 begin/commit/rollback；嵌套进入者（含
          内部写方法自动加入）只共享该事务，不再 begin。
        - rollback-only：任一嵌套作用域失败（含公共写方法的写前校验）即
          标记整个组合事务；嵌套异常即使被调用方捕获，外层也在真正
          commit 前抛 AccountStateError 并整体回滚，不提交部分更新。
        - 需要逐笔独立提交/回滚的语义，请逐笔单独开事务，
          不要包进同一组合事务。
        """
        self._ensure_writable()
        with self._txn():
            yield self._conn

    @contextmanager
    def _txn(self):
        """深度计数 + rollback-only 标志的组合事务。

        DuckDB 无 SAVEPOINT，且 Python 异常不会自动 abort 事务：嵌套失败
        只标 poison 并重抛；owner 在 commit 前检测到标志则回滚并抛
        AccountStateError；finally 归零深度/标志，供下一笔事务正常进行。
        """
        if self._txn_depth > 0:
            self._txn_depth += 1
            try:
                yield self._conn
            except BaseException:
                self._txn_poisoned = True
                raise
            finally:
                self._txn_depth -= 1
        else:
            self._txn_depth = 1
            self._txn_poisoned = False
            self._conn.begin()
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                if self._txn_poisoned:
                    self._conn.rollback()
                    raise AccountStateError(
                        "组合事务内有嵌套操作失败（rollback-only）：外层已整体"
                        "回滚，未提交任何更新；请修正失败原因后重试整个组合事务"
                    )
                self._conn.commit()
            finally:
                self._txn_depth = 0
                self._txn_poisoned = False

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @classmethod
    def create(cls, path: str, *, account_id: str, broker: str = "",
               note: str = "", exist_ok: bool = False) -> "AccountLedger":
        """建立新账本库；已存在文件默认拒绝，避免覆盖真实账本。"""
        account_id = require_text(account_id, "account_id")
        broker = optional_text(broker, "broker")
        note = optional_text(note, "note")
        if os.path.exists(path):
            if not exist_ok:
                raise AccountConflictError(
                    f"账本文件已存在: {path}——不会覆盖已有文件；如确需重建请先手动处理"
                )
            return cls.open(path)
        assert_duckdb_file(path, kind="实盘账本")
        conn = duckdb.connect(path)
        try:
            with db_transaction(conn):
                conn.execute(_ACCOUNT_SCHEMA_SQL)
                for key, value in (
                    ("schema_version", str(SCHEMA_VERSION)),
                    ("account_id", account_id),
                    ("broker", broker),
                    ("note", note),
                    ("created_at", _now()),
                ):
                    conn.execute(
                        "INSERT INTO acct_meta (key, value) VALUES (?, ?)",
                        (key, value),
                    )
                _insert_audit(conn, "create_ledger", "ledger", account_id, {
                    "path": str(path), "schema_version": SCHEMA_VERSION,
                })
        except BaseException:
            conn.close()
            raise
        return cls(conn, path, read_only=False)

    @classmethod
    def open(cls, path: str, *, read_only: bool = False) -> "AccountLedger":
        """打开已有账本库；路径不存在不悄悄创建，旧 schema 明确拒绝。"""
        if not os.path.exists(path):
            raise LedgerPathError(
                f"账本文件不存在: {path}——open 不会自动创建；请先 create/init"
            )
        try:
            assert_duckdb_file(path, kind="实盘账本")
        except ValueError as exc:
            raise LedgerSchemaError(str(exc)) from None
        conn = duckdb.connect(path, read_only=read_only)
        ledger = cls(conn, path, read_only=read_only)
        try:
            ledger._verify_schema()
            if not read_only:
                with db_transaction(conn):
                    conn.execute(_ACCOUNT_SCHEMA_SQL)
        except BaseException:
            conn.close()
            raise
        return ledger

    def _verify_schema(self):
        tables = {
            row[0] for row in self._conn.execute(
                "SELECT table_name FROM information_schema.tables"
            ).fetchall()
        }
        if "acct_meta" not in tables:
            if "ledger_meta" in tables:
                version = None
                try:
                    row = self._conn.execute(
                        "SELECT value FROM ledger_meta WHERE key = 'schema_version'"
                    ).fetchone()
                    version = row[0] if row else None
                except duckdb.Error:
                    version = None
                raise LedgerSchemaError(
                    f"{self.path} 是旧 V1 实盘账本（ledger_meta schema_version="
                    f"{version or '未知'}），V2 不做迁移——请使用新的账本文件路径；"
                    "该文件不会被修改"
                )
            raise LedgerSchemaError(
                f"{self.path} 不是实盘 V2 账本库（缺少 acct_meta；"
                "可能是回测结果库或其他 DuckDB 文件），拒绝打开"
            )
        version = self._meta("schema_version")
        if version is None:
            raise LedgerSchemaError(
                f"{self.path} 缺少 schema_version，不是有效的实盘 V2 账本库"
            )
        if version != str(SCHEMA_VERSION):
            raise LedgerSchemaError(
                f"账本 schema_version={version} 不受支持"
                f"（当前版本 {SCHEMA_VERSION}）——请使用对应版本的 ddup"
            )

    def _ensure_writable(self):
        if self.read_only:
            raise AccountStateError("只读连接不能写入账本")

    def _meta(self, key: str) -> str | None:
        row = self._conn.execute(
            "SELECT value FROM acct_meta WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None

    # ── 快照：导入 / 确认 / 接管（§3.1）──

    def import_snapshot(self, *, as_of, source, import_id, cash=None,
                        cash_balance=None, cash_available=None, holdings=None,
                        note: str = "", raw=None,
                        tolerance: float = DEFAULT_CASH_TOLERANCE,
                        ) -> ImportSnapshotResult:
        """导入完整券商快照。

        - 身份 = (source, import_id)：同身份同内容幂等返回已有快照；
          同身份异内容抛 AccountConflictError，不静默覆盖。
        - 首次导入无基准：直接确认为期初（opening），不要求补历史。
        - 有基准时与投影比对：clean 自动确认；有差异存为 candidate，
          正式状态不偷换，等待补事实 reconcile 或接管。
        - cash 简化输入按 余额 = 可用现金、冻结 = 0 解释；与
          cash_balance/cash_available 互斥。
        - holdings 缺失即拒绝（缺 holdings 不等于空仓）；空仓传 []。
        """
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界：组合事务内校验失败同样 rollback-only
            source = require_text(source, "source")
            import_id = require_text(import_id, "import_id")
            as_of = validate_date_format(as_of, "as_of")
            note = optional_text(note, "note")
            tolerance = require_non_negative(tolerance, "tolerance")
            balance, available = _parse_cash_input(cash, cash_balance, cash_available)
            holdings_map = _normalize_holdings(holdings, "import_snapshot")
            payload = {
                "as_of": as_of,
                "cash_balance": _round6(balance),
                "cash_available": _round6(available),
                "holdings": [
                    {"symbol": s.symbol, "shares": s.shares,
                     "available": s.available,
                     "cost": None if s.cost is None else _round6(s.cost)}
                    for s in sorted(holdings_map.values(), key=lambda h: h.symbol)
                ],
            }
            content_hash = _content_hash(payload)
            raw_json = raw if isinstance(raw, str) else json.dumps(
                payload, ensure_ascii=False, sort_keys=True, default=str)

        try:
            with self._txn():
                existing = self._conn.execute(
                    "SELECT snapshot_id, content_hash FROM acct_snapshots"
                    " WHERE source = ? AND import_id = ?", (source, import_id)
                ).fetchone()
                if existing is not None:
                    if existing[1] != content_hash:
                        raise AccountConflictError(
                            f"快照身份 ({source}, {import_id}) 已存在且内容不同"
                            f"（已有 content_hash={existing[1][:12]}…，"
                            f"本次 content_hash={content_hash[:12]}…）——"
                            "拒绝静默覆盖；如为新一轮导入请使用新的 import_id"
                        )
                    snapshot = self._load_snapshot(existing[0])
                    return ImportSnapshotResult(
                        snapshot=snapshot, diff=None,
                        confirmed=snapshot.status == SNAPSHOT_CONFIRMED,
                        action="idempotent_noop",
                    )

                base = self._base_snapshot()
                now = _now()
                if base is None:
                    snapshot_id = self._insert_snapshot(
                        status=SNAPSHOT_CONFIRMED, as_of=as_of, captured_at=now,
                        source=source, import_id=import_id, balance=balance,
                        available=available, holdings=holdings_map, note=note,
                        raw_json=raw_json, content_hash=content_hash)
                    _insert_audit(self._conn, "snapshot_imported", "snapshot",
                                  str(snapshot_id),
                                  {"opening": True, "as_of": as_of})
                    _insert_audit(self._conn, "snapshot_auto_confirmed",
                                  "snapshot", str(snapshot_id),
                                  {"reason": "opening", "as_of": as_of})
                    return ImportSnapshotResult(
                        snapshot=self._load_snapshot(snapshot_id), diff=None,
                        confirmed=True, action="opening_confirmed")

                if as_of < base.as_of:
                    raise AccountValidationError(
                        f"快照 as_of={as_of} 早于当前确认基准 {base.as_of}"
                        "——请按时间顺序导入"
                    )
                diff = self._diff_against(base, as_of, balance, holdings_map,
                                          tolerance)
                if diff.clean:
                    snapshot_id = self._insert_snapshot(
                        status=SNAPSHOT_CONFIRMED, as_of=as_of, captured_at=now,
                        source=source, import_id=import_id, balance=balance,
                        available=available, holdings=holdings_map, note=note,
                        raw_json=raw_json, content_hash=content_hash)
                    _insert_audit(self._conn, "snapshot_imported", "snapshot",
                                  str(snapshot_id), {"as_of": as_of})
                    _insert_audit(self._conn, "snapshot_auto_confirmed",
                                  "snapshot", str(snapshot_id),
                                  {"reason": "projection_match", "as_of": as_of})
                    self._expire_stale_candidates(as_of)
                    return ImportSnapshotResult(
                        snapshot=self._load_snapshot(snapshot_id), diff=diff,
                        confirmed=True, action="imported_confirmed")

                snapshot_id = self._insert_snapshot(
                    status=SNAPSHOT_CANDIDATE, as_of=as_of, captured_at=now,
                    source=source, import_id=import_id, balance=balance,
                    available=available, holdings=holdings_map, note=note,
                    raw_json=raw_json, content_hash=content_hash)
                self._store_diff_rows(snapshot_id, diff)
                _insert_audit(self._conn, "snapshot_imported", "snapshot",
                              str(snapshot_id),
                              {"as_of": as_of, "candidate": True,
                               "diff": diff.summary()})
                return ImportSnapshotResult(
                    snapshot=self._load_snapshot(snapshot_id), diff=diff,
                    confirmed=False, action="stored_candidate")
        except LiveAccountError as exc:
            self._audit_rejection("import_snapshot", exc)
            raise

    def reconcile_candidate(self, snapshot_id: int, *,
                            tolerance: float = DEFAULT_CASH_TOLERANCE,
                            ) -> SnapshotReviewResult:
        """重算候选差异：补齐事实后 clean 则确认，否则返回剩余差异。"""
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界
            snapshot_id = require_whole_int(snapshot_id, "snapshot_id", minimum=1)
            tolerance = require_non_negative(tolerance, "tolerance")
        try:
            with self._txn():
                snapshot = self._require_snapshot(snapshot_id)
                if snapshot.status != SNAPSHOT_CANDIDATE:
                    raise AccountStateError(
                        f"快照 {snapshot_id} 状态为 {snapshot.status}，不是待确认候选"
                    )
                base = self._base_snapshot()
                if base is None or snapshot.as_of <= base.as_of:
                    raise AccountStateError(
                        f"候选快照 {snapshot_id}（as_of={snapshot.as_of}）已被更新的"
                        f"确认基准 {base.as_of if base else '无'} 超越——请重新导入"
                    )
                diff = self._diff_against(base, snapshot.as_of,
                                          snapshot.cash_balance,
                                          snapshot.holdings, tolerance)
                if diff.clean:
                    self._conn.execute(
                        "UPDATE acct_snapshots SET status = ? WHERE snapshot_id = ?",
                        (SNAPSHOT_CONFIRMED, snapshot_id),
                    )
                    self._delete_diff_rows(snapshot_id)
                    _insert_audit(self._conn, "snapshot_confirmed", "snapshot",
                                  str(snapshot_id),
                                  {"resolution": "facts_recorded",
                                   "as_of": snapshot.as_of})
                    self._expire_stale_candidates(snapshot.as_of)
                    return SnapshotReviewResult(
                        snapshot=self._load_snapshot(snapshot_id), diff=diff,
                        confirmed=True, action="reconciled_confirmed")
                self._delete_diff_rows(snapshot_id)
                self._store_diff_rows(snapshot_id, diff)
                _insert_audit(self._conn, "candidate_refreshed", "snapshot",
                              str(snapshot_id),
                              {"remaining": diff.summary()})
                return SnapshotReviewResult(
                    snapshot=self._load_snapshot(snapshot_id), diff=diff,
                    confirmed=False, action="reconciled_pending")
        except LiveAccountError as exc:
            self._audit_rejection("reconcile_candidate", exc)
            raise

    def takeover_candidate(self, snapshot_id: int, *, reason: str,
                           ) -> AccountSnapshot:
        """接管：以券商事实为新基准，允许与历史不同但不伪造成交。

        差异前后值与原因写入接管记录；新旧基准之间的未决事实标记
        superseded（其经济效果已包含在新基准内，不重复入账）。
        接管缺口使 PnL 标记 incomplete。reason 必填。
        """
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界
            reason = require_text(reason, "reason")
            snapshot_id = require_whole_int(snapshot_id, "snapshot_id", minimum=1)
        try:
            with self._txn():
                snapshot = self._require_snapshot(snapshot_id)
                if snapshot.status != SNAPSHOT_CANDIDATE:
                    raise AccountStateError(
                        f"快照 {snapshot_id} 状态为 {snapshot.status}，不是待接管候选"
                    )
                newest = self._conn.execute(
                    "SELECT snapshot_id FROM acct_snapshots"
                    " WHERE status = ? ORDER BY as_of DESC, snapshot_id DESC"
                    " LIMIT 1", (SNAPSHOT_CANDIDATE,)
                ).fetchone()
                if newest is None or newest[0] != snapshot_id:
                    raise AccountStateError(
                        f"存在更新候选（snapshot_id={newest[0] if newest else '?'}），"
                        f"请先处理它再接管 {snapshot_id}"
                    )
                base = self._base_snapshot()
                if base is None or snapshot.as_of <= base.as_of:
                    raise AccountStateError(
                        f"候选快照 {snapshot_id}（as_of={snapshot.as_of}）不晚于当前"
                        f"基准 {base.as_of if base else '无'}，无需接管"
                    )
                diff = self._diff_against(base, snapshot.as_of,
                                          snapshot.cash_balance,
                                          snapshot.holdings,
                                          DEFAULT_CASH_TOLERANCE)
                if diff.clean:
                    raise AccountStateError(
                        f"候选快照 {snapshot_id} 的差异已被已记录事实完全解释，"
                        "无需接管——请改用 reconcile_candidate 确认")
                now = _now()
                diff_json = json.dumps({
                    "base_snapshot_id": base.snapshot_id,
                    "base_as_of": base.as_of,
                    "cash": None if diff.cash is None else {
                        "expected": diff.cash.expected, "actual": diff.cash.actual,
                        "delta": diff.cash.delta},
                    "holdings": {
                        s: {"expected": d.expected, "actual": d.actual}
                        for s, d in sorted(diff.holding_diffs.items())
                    },
                    "reason": reason,
                }, ensure_ascii=False, sort_keys=True)
                takeover_id = self._next_id("acct_takeovers", "takeover_id")
                self._conn.execute(
                    "INSERT INTO acct_takeovers (takeover_id, snapshot_id, as_of,"
                    " reason, diff_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (takeover_id, snapshot_id, snapshot.as_of, reason, diff_json,
                     now),
                )
                for table, id_col in (("acct_fills", "fill_id"),
                                      ("acct_cash_flows", "flow_id"),
                                      ("acct_share_adjustments", "adj_id")):
                    self._conn.execute(
                        f"UPDATE {table} SET superseded_by = ?"
                        f" WHERE superseded_by IS NULL AND date > ? AND date <= ?",
                        (takeover_id, base.as_of, snapshot.as_of),
                    )
                self._conn.execute(
                    "UPDATE acct_snapshots SET status = ? WHERE snapshot_id = ?",
                    (SNAPSHOT_CONFIRMED, snapshot_id),
                )
                self._delete_diff_rows(snapshot_id)
                self._expire_stale_candidates(snapshot.as_of)
                _insert_audit(self._conn, "snapshot_takeover", "snapshot",
                              str(snapshot_id),
                              {"takeover_id": takeover_id, "reason": reason,
                               "diff": diff.summary()})
                return self._load_snapshot(snapshot_id)
        except LiveAccountError as exc:
            self._audit_rejection("takeover_candidate", exc)
            raise

    def confirmed_snapshot(self) -> AccountSnapshot | None:
        """最新确认快照（操作基础）。"""
        return self._base_snapshot()

    def pending_candidates(self) -> list[AccountSnapshot]:
        rows = self._conn.execute(
            "SELECT snapshot_id FROM acct_snapshots WHERE status = ?"
            " ORDER BY as_of, snapshot_id", (SNAPSHOT_CANDIDATE,)
        ).fetchall()
        return [self._load_snapshot(row[0]) for row in rows]

    def snapshots(self, status: str | None = None) -> list[AccountSnapshot]:
        if status is not None and status not in {
                SNAPSHOT_CANDIDATE, SNAPSHOT_CONFIRMED, SNAPSHOT_EXPIRED}:
            raise AccountValidationError(f"非法快照状态过滤: {status!r}")
        sql = "SELECT snapshot_id FROM acct_snapshots"
        params: tuple = ()
        if status is not None:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY as_of, snapshot_id"
        return [self._load_snapshot(row[0]) for row in
                self._conn.execute(sql, params).fetchall()]

    # ── 成交：单笔 / 批量 / 修正（§3.2）──

    def record_fill(self, *, date, symbol, side, quantity, price,
                    fee_commission: float = 0.0, fee_tax: float = 0.0,
                    fee_transfer: float = 0.0, fee_other: float = 0.0,
                    source, trade_id, plan_id: str = "", order_ref: str = "",
                    external: bool | None = None, note: str = "") -> FillRecord:
        """单笔成交入账：(source, trade_id) 身份幂等；同身份异内容拒绝。

        无券商 ID 的批量导入请用 import_fills（batch+row 身份）。
        """
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界：组合事务内校验失败同样 rollback-only
            row = {"date": date, "symbol": symbol, "side": side,
                   "quantity": quantity, "price": price,
                   "fee_commission": fee_commission, "fee_tax": fee_tax,
                   "fee_transfer": fee_transfer, "fee_other": fee_other,
                   "trade_id": require_text(trade_id, "trade_id"),
                   "plan_id": optional_text(plan_id, "plan_id"),
                   "order_ref": optional_text(order_ref, "order_ref"),
                   "note": optional_text(note, "note"), "external": external}
            normalized = _normalize_fill_row(row, "record_fill")
        result = self._insert_fills(source, [normalized], batch_info=None,
                                    action="fill_recorded")
        return result.fills[0] if result.fills else self._load_fill(
            source, normalized["trade_id"])

    def import_fills(self, source: str, batch_id: str, rows,
                     *, default_date=None, note: str = "") -> ImportFillsResult:
        """批量成交导入：整批原子，身份与内容双重校验。

        - 批身份 (source, batch_id)：同批同内容重入幂等（appended=0）；
          同批异内容报冲突，防止同批重入被改写。
        - 行身份 (source, trade_id)：rows 缺 trade_id 时按
          ``{batch_id}#r{行号}`` 稳定生成；不同 trade_id 的相同成交都保留。
        - 任一行非法或与投影矛盾（超卖）时整批回滚，不留半批。
        """
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界：组合事务内校验失败同样 rollback-only
            source = require_text(source, "source")
            batch_id = require_text(batch_id, "batch_id")
            note = optional_text(note, "note")
            if default_date is not None:
                default_date = validate_date_format(default_date, "default_date")
            if not isinstance(rows, (list, tuple)):
                raise AccountValidationError(
                    f"rows 必须为成交列表: {type(rows).__name__}")
            normalized = []
            for index, row in enumerate(rows, start=1):
                if not isinstance(row, dict):
                    raise AccountValidationError(f"第 {index} 行: 非法条目 {row!r}")
                item = dict(row)
                if item.get("date") is None:
                    item["date"] = default_date
                if item.get("trade_id") is None:
                    item["trade_id"] = f"{batch_id}#r{index}"
                normalized.append(_normalize_fill_row(item, f"rows 第 {index} 行"))
            payload = [_fill_canonical(row) for row in normalized]
            batch_info = {
                "source": source, "batch_id": batch_id,
                "content_hash": _content_hash(payload),
                "n_rows": len(normalized),
            }
        return self._insert_fills(source, normalized, batch_info=batch_info,
                                  action="fills_batch_imported", note=note)

    def find_fill(self, source: str, trade_id: str):
        """按 (source, trade_id) 查成交身份；不存在返回 None。

        供计划层在回报前判定重复（幂等 no-op vs 身份冲突）。"""
        row = self._conn.execute(
            f"SELECT {_FILL_COLUMNS} FROM acct_fills"
            " WHERE source = ? AND trade_id = ?", (source, trade_id),
        ).fetchone()
        return self._fill_from_row(row) if row is not None else None

    def fills(self, *, plan_id: str | None = None,
              order_ref: str | None = None) -> list[FillRecord]:
        """按入账顺序返回全部成交（含 superseded 标记），可按计划/订单过滤。"""
        sql = f"SELECT {_FILL_COLUMNS} FROM acct_fills"
        clauses, params = [], []
        if plan_id is not None:
            clauses.append("plan_id = ?")
            params.append(plan_id)
        if order_ref is not None:
            clauses.append("order_ref = ?")
            params.append(order_ref)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY fill_id"
        return [self._fill_from_row(row) for row in
                self._conn.execute(sql, params).fetchall()]

    def correct_fill(self, *, target_source, target_trade_id, event_source,
                     event_id, date, fields, note: str = "") -> FillCorrection:
        """修正成交：引用原记录的 correction 事件，投影按修正后内容重放。"""
        self._ensure_writable()
        try:
            with self._txn():  # 组合边界：任何失败（含校验）均 rollback-only
                return self._correct(target_source=target_source,
                                     target_trade_id=target_trade_id,
                                     event_source=event_source, event_id=event_id,
                                     date=date, kind=CORRECTION, fields=fields,
                                     note=note)
        except LiveAccountError as exc:
            self._audit_rejection("fill_correction", exc)
            raise

    def void_fill(self, *, target_source, target_trade_id, event_source,
                  event_id, date, reason: str) -> FillCorrection:
        """作废成交：void 事件让投影忽略该笔；历史行保留可审计。"""
        self._ensure_writable()
        try:
            with self._txn():
                return self._correct(target_source=target_source,
                                     target_trade_id=target_trade_id,
                                     event_source=event_source, event_id=event_id,
                                     date=date, kind=VOID, fields={},
                                     note=require_text(reason, "reason"))
        except LiveAccountError as exc:
            self._audit_rejection("fill_correction", exc)
            raise

    def fill_corrections(self) -> list[FillCorrection]:
        rows = self._conn.execute(
            "SELECT c.correction_id, c.fill_id, f.trade_id, c.kind, c.date,"
            " c.fields_json, c.source, c.event_id, c.note, c.created_at"
            " FROM acct_fill_corrections c JOIN acct_fills f ON f.fill_id = c.fill_id"
            " ORDER BY c.correction_id"
        ).fetchall()
        return [FillCorrection(
            correction_id=r[0], fill_id=r[1], target_trade_id=r[2], kind=r[3],
            date=r[4], fields=json.loads(r[5]), source=r[6], event_id=r[7],
            note=r[8], created_at=r[9],
        ) for r in rows]

    # ── 现金流与股数调整（§3.3 / §3.4）──

    def record_cash_flow(self, *, date, category, amount, symbol: str = "",
                         source, event_id, note: str = "") -> CashFlow:
        """分类现金流：sign 规则按类别强制，不明差额不得冒充入金。"""
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界：组合事务内校验失败同样 rollback-only
            date = validate_date_format(date, "date")
            category = require_text(category, "category")
            if category not in CASH_FLOW_CATEGORIES:
                raise AccountValidationError(
                    f"非法现金流类别 {category!r}（允许 {sorted(CASH_FLOW_CATEGORIES)}）")
            amount = require_finite(amount, f"{category} amount")
            sign = _CATEGORY_SIGN[category]
            if sign == "positive" and amount <= 0:
                raise AccountValidationError(f"{category} 金额必须为正: {amount!r}")
            if sign == "negative" and amount >= 0:
                raise AccountValidationError(f"{category} 金额必须为负: {amount!r}")
            if sign == "nonzero" and amount == 0:
                raise AccountValidationError(f"{category} 金额不能为零")
            symbol = optional_text(symbol, "symbol")
            source = require_text(source, "source")
            event_id = require_text(event_id, "event_id")
            note = optional_text(note, "note")
            canonical = {"date": date, "category": category,
                         "amount": _round6(amount), "symbol": symbol,
                         "note": note}
        try:
            with self._txn():
                self._require_base("record_cash_flow")
                existing = self._identity_row(
                    "acct_cash_flows", "flow_id", source, event_id,
                    "date, category, amount, symbol")
                if existing is not None:
                    stored = dict(zip(("date", "category", "amount", "symbol"),
                                      existing[2:]))
                    if not _same_content(stored, canonical):
                        raise _content_conflict("cash_flow", (source, event_id),
                                                stored, canonical)
                    return self._load_cash_flow(existing[0])
                pre_base = date <= self._base_snapshot().as_of
                flow_id = self._next_id("acct_cash_flows", "flow_id")
                now = _now()
                self._conn.execute(
                    "INSERT INTO acct_cash_flows (flow_id, date, category,"
                    " amount, symbol, source, event_id, note, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (flow_id, date, category, amount, symbol, source, event_id,
                     note, now),
                )
                _insert_audit(self._conn, "cash_flow_recorded", "cash_flow",
                              str(flow_id),
                              {"category": category, "amount": amount,
                               "date": date, "pre_base": pre_base})
                return self._load_cash_flow(flow_id)
        except LiveAccountError as exc:
            self._audit_rejection("record_cash_flow", exc)
            raise

    def record_share_adjustment(self, *, date, symbol, delta_shares, category,
                                reason: str, source, event_id) -> ShareAdjustment:
        """股数调整（送转/转增等实际到账）：只改股数，不伪造成交或现金流。"""
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界
            date = validate_date_format(date, "date")
            symbol = require_text(symbol, "symbol")
            delta = require_whole_int(delta_shares, "delta_shares")
            if delta == 0:
                raise AccountValidationError("delta_shares 不能为零")
            category = require_text(category, "category")
            if category not in SHARE_ADJ_CATEGORIES:
                raise AccountValidationError(
                    f"非法股数调整类别 {category!r}"
                    f"（允许 {sorted(SHARE_ADJ_CATEGORIES)}）")
            reason = optional_text(reason, "reason")
            source = require_text(source, "source")
            event_id = require_text(event_id, "event_id")
            canonical = {"date": date, "symbol": symbol,
                         "delta_shares": delta, "category": category}
        try:
            with self._txn():
                self._require_base("record_share_adjustment")
                existing = self._identity_row(
                    "acct_share_adjustments", "adj_id", source, event_id,
                    "date, symbol, delta_shares, category")
                if existing is not None:
                    stored = dict(zip(("date", "symbol", "delta_shares", "category"),
                                      existing[2:]))
                    if not _same_content(stored, canonical):
                        raise _content_conflict("share_adjustment", (source, event_id),
                                                stored, canonical)
                    return self._load_share_adjustment(existing[0])
                pre_base = date <= self._base_snapshot().as_of
                self._prevalidate_adjustment(date, symbol, delta)
                adj_id = self._next_id("acct_share_adjustments", "adj_id")
                now = _now()
                self._conn.execute(
                    "INSERT INTO acct_share_adjustments (adj_id, date, symbol,"
                    " delta_shares, category, reason, source, event_id,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (adj_id, date, symbol, delta, category, reason, source,
                     event_id, now),
                )
                _insert_audit(self._conn, "share_adjustment_recorded",
                              "share_adjustment", str(adj_id),
                              {"symbol": symbol, "delta": delta, "date": date,
                               "pre_base": pre_base})
                return self._load_share_adjustment(adj_id)
        except LiveAccountError as exc:
            self._audit_rejection("record_share_adjustment", exc)
            raise

    def cash_flows(self) -> list[CashFlow]:
        rows = self._conn.execute(
            "SELECT flow_id, date, category, amount, symbol, source, event_id,"
            " note, created_at, superseded_by FROM acct_cash_flows"
            " ORDER BY flow_id"
        ).fetchall()
        return [CashFlow(*row) for row in rows]

    def share_adjustments(self) -> list[ShareAdjustment]:
        rows = self._conn.execute(
            "SELECT adj_id, date, symbol, delta_shares, category, reason,"
            " source, event_id, created_at, superseded_by"
            " FROM acct_share_adjustments ORDER BY adj_id"
        ).fetchall()
        return [ShareAdjustment(*row) for row in rows]

    # ── 持仓管理政策（§3.4）──

    def set_holding_policy(self, symbol: str, policy: str, *, reason: str = "",
                           expire_date=None, anchor_date=None,
                           anchor_price=None) -> HoldingPolicy:
        """设置持仓政策；managed 必须显式提供入场锚点，未知锚点保持 observe。"""
        self._ensure_writable()
        with self._txn():  # 校验纳入组合边界：组合事务内校验失败同样 rollback-only
            symbol = require_text(symbol, "symbol")
            policy = require_text(policy, "policy")
            if policy not in HOLDING_POLICIES:
                raise AccountValidationError(
                    f"非法持仓政策 {policy!r}（允许 {sorted(HOLDING_POLICIES)}）")
            reason = optional_text(reason, "reason")
            expire = None
            if expire_date is not None:
                expire = validate_date_format(expire_date, "expire_date")
            anchor = None
            if anchor_date is not None:
                anchor = validate_date_format(anchor_date, "anchor_date")
            price = None
            if anchor_price is not None:
                price = require_positive_price(anchor_price, "anchor_price")
            if policy == POLICY_MANAGED and anchor is None:
                raise AccountValidationError(
                    f"{symbol} 设为 managed 需要显式入场锚点 anchor_date；"
                    "锚点未知请保持 observe（或补锚点后再改 managed），不编造日期"
                )
            if policy == POLICY_MANAGED and expire is not None:
                raise AccountValidationError(
                    "managed 不支持 expire_date（保留期限仅用于 exit_only/observe）")
        try:
            with self._txn():
                now = _now()
                self._conn.execute(
                    "INSERT OR REPLACE INTO acct_holding_policies (symbol,"
                    " policy, reason, expire_date, anchor_date, anchor_price,"
                    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (symbol, policy, reason, expire, anchor, price, now),
                )
                _insert_audit(self._conn, "policy_set", "holding", symbol,
                              {"policy": policy, "reason": reason,
                               "expire_date": expire, "anchor_date": anchor})
                return self.holding_policy(symbol)
        except LiveAccountError as exc:
            self._audit_rejection("set_holding_policy", exc)
            raise

    def holding_policy(self, symbol: str) -> HoldingPolicy | None:
        row = self._conn.execute(
            "SELECT symbol, policy, reason, expire_date, anchor_date,"
            " anchor_price, updated_at FROM acct_holding_policies WHERE symbol = ?",
            (symbol,),
        ).fetchone()
        return HoldingPolicy(*row) if row else None

    def effective_policy(self, symbol: str, on_date: str | None = None) -> str:
        """生效政策：无记录默认 observe；exit_only/observe 到期后恢复规则：
        有锚点恢复 managed，无锚点保持 observe（§3.4 明确恢复规则）。"""
        policy = self.holding_policy(symbol)
        if policy is None:
            return POLICY_OBSERVE
        reference = on_date
        if reference is None:
            base = self._base_snapshot()
            reference = base.as_of if base is not None else "99999999"
        if (policy.expire_date is not None and reference >= policy.expire_date
                and policy.policy in (POLICY_EXIT_ONLY, POLICY_OBSERVE)):
            return POLICY_MANAGED if policy.anchor_date else POLICY_OBSERVE
        return policy.policy

    def effective_policies(self, on_date: str | None = None) -> dict:
        """当前持仓（投影）与已登记政策行的并集 -> 生效政策。"""
        symbols = set()
        try:
            symbols |= set(self.projection().holdings)
        except AccountStateError:
            pass
        symbols |= {
            row[0] for row in self._conn.execute(
                "SELECT symbol FROM acct_holding_policies").fetchall()
        }
        return {s: self.effective_policy(s, on_date) for s in sorted(symbols)}

    # ── 投影 / 权益 / PnL（§3.3）──

    def projection(self, as_of: str | None = None) -> ProjectedState:
        """从最新确认基准 + 后续事实投影账户状态；无基准时报错。"""
        base = self._require_base("projection")
        if as_of is not None:
            as_of = validate_date_format(as_of, "as_of")
        return self._project(base, as_of)

    def equity(self, prices=None) -> EquityView:
        """权益 = 现金 + Σ持仓×价格；缺价则 holdings_value/total 为 None。"""
        return self._equity_of(self.projection(), prices)

    def investment_pnl(self, *, opening_prices=None,
                       closing_prices=None) -> PnLView:
        """投资 PnL = 期末权益 - 期初权益 - 净外部出入金。

        期初 = 首个确认快照；分红计投资收益，费用/税费自然扣减权益；
        待确认候选、接管缺口或缺价时 complete=False，不伪造精确收益率。
        """
        first = self._conn.execute(
            "SELECT snapshot_id FROM acct_snapshots WHERE status = ?"
            " ORDER BY as_of, snapshot_id LIMIT 1", (SNAPSHOT_CONFIRMED,)
        ).fetchone()
        if first is None:
            return PnLView(None, None, None, None, 0.0, None, False,
                           ("no_confirmed_snapshot",))
        opening = self._load_snapshot(first[0])
        reasons: list[str] = []
        if opening.holdings:
            opening_eq = self._value_holdings(
                opening.holdings, opening_prices, "missing_opening_price",
                reasons)
            opening_equity = (opening.cash_balance + opening_eq
                              if opening_eq is not None else None)
        else:
            opening_equity = opening.cash_balance

        try:
            proj = self.projection()
        except AccountStateError:
            closing_equity, closing_as_of = None, None
        else:
            closing_as_of = proj.projected_through
            closing_view = self._equity_of(proj, closing_prices)
            if closing_view.total is None:
                reasons.extend(
                    f"missing_closing_price:{s}" for s in closing_view.missing_prices)
            closing_equity = closing_view.total

        for candidate in self.pending_candidates():
            reasons.append(f"pending_candidate:{candidate.snapshot_id}")
        reasons.extend(
            f"takeover_gap:{row[0]}" for row in self._conn.execute(
                "SELECT snapshot_id FROM acct_takeovers ORDER BY takeover_id"
            ).fetchall())
        net_external = self._conn.execute(
            "SELECT COALESCE(SUM(amount), 0) FROM acct_cash_flows"
            " WHERE superseded_by IS NULL AND category IN (?, ?)",
            (CASH_DEPOSIT, CASH_WITHDRAWAL),
        ).fetchone()[0]
        pnl = None
        if opening_equity is not None and closing_equity is not None and not reasons:
            pnl = closing_equity - opening_equity - net_external
        return PnLView(
            opening_as_of=opening.as_of, closing_as_of=closing_as_of,
            opening_equity=opening_equity, closing_equity=closing_equity,
            net_external_flows=net_external, investment_pnl=pnl,
            complete=not reasons, reasons=tuple(reasons),
        )

    # ── 接管与审计查询 ──

    def takeovers(self) -> list[TakeoverRecord]:
        rows = self._conn.execute(
            "SELECT takeover_id, snapshot_id, as_of, reason, diff_json,"
            " created_at FROM acct_takeovers ORDER BY takeover_id"
        ).fetchall()
        return [TakeoverRecord(*row) for row in rows]

    def audit_events(self, limit: int = 200) -> list[AuditEvent]:
        rows = self._conn.execute(
            "SELECT event_seq, event_at, actor, action, subject_type, subject_id,"
            " detail_json FROM acct_audit ORDER BY event_seq DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [AuditEvent(*row) for row in rows]

    def status(self) -> LedgerStatus:
        """确认快照 + 未决差异 + 投影概览；只读，不改变任何状态。"""
        candidates = []
        for snapshot in self.pending_candidates():
            diff = self._load_diff(snapshot.snapshot_id)
            candidates.append(PendingCandidate(
                snapshot_id=snapshot.snapshot_id, as_of=snapshot.as_of,
                source=snapshot.source, import_id=snapshot.import_id,
                diff=diff))
        try:
            projected = self.projection()
        except AccountStateError:
            projected = None
        counts = {"snapshots": self._count("acct_snapshots")}
        for table, key in (("acct_fills", "fills"),
                           ("acct_cash_flows", "cash_flows"),
                           ("acct_share_adjustments", "share_adjustments"),
                           ("acct_fill_corrections", "fill_corrections"),
                           ("acct_takeovers", "takeovers"),
                           ("acct_audit", "audit_events")):
            counts[key] = self._count(table)
        gap_count = counts["takeovers"]
        return LedgerStatus(
            schema_version=SCHEMA_VERSION, account_id=self.account_id,
            broker=self.broker, created_at=self._meta("created_at") or "",
            confirmed_snapshot=self._base_snapshot(), pending=tuple(candidates),
            history_incomplete=gap_count > 0, gap_count=gap_count,
            counts=counts, projected=projected,
        )

    # ── 内部：快照存取与差异 ──

    def _insert_snapshot(self, *, status, as_of, captured_at, source, import_id,
                         balance, available, holdings, note, raw_json,
                         content_hash) -> int:
        snapshot_id = self._next_id("acct_snapshots", "snapshot_id")
        self._conn.execute(
            "INSERT INTO acct_snapshots (snapshot_id, status, as_of,"
            " captured_at, source, import_id, cash_balance, cash_available,"
            " note, raw_json, content_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (snapshot_id, status, as_of, captured_at, source, import_id,
             balance, available, note, raw_json, content_hash),
        )
        if holdings:
            self._conn.executemany(
                "INSERT INTO acct_snapshot_holdings (snapshot_id, symbol,"
                " shares, available, cost) VALUES (?, ?, ?, ?, ?)",
                [(snapshot_id, h.symbol, h.shares, h.available, h.cost)
                 for h in sorted(holdings.values(), key=lambda h: h.symbol)],
            )
        return snapshot_id

    def _base_snapshot(self) -> AccountSnapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_id FROM acct_snapshots WHERE status = ?"
            " ORDER BY as_of DESC, snapshot_id DESC LIMIT 1",
            (SNAPSHOT_CONFIRMED,),
        ).fetchone()
        return self._load_snapshot(row[0]) if row else None

    def _require_snapshot(self, snapshot_id: int) -> AccountSnapshot:
        snapshot = self._load_snapshot(snapshot_id)
        if snapshot is None:
            raise AccountStateError(f"快照不存在: snapshot_id={snapshot_id}")
        return snapshot

    def _load_snapshot(self, snapshot_id: int) -> AccountSnapshot | None:
        row = self._conn.execute(
            "SELECT snapshot_id, status, as_of, captured_at, source, import_id,"
            " cash_balance, cash_available, note, raw_json, content_hash"
            " FROM acct_snapshots WHERE snapshot_id = ?", (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        holdings = {
            h[0]: SnapshotHolding(symbol=h[0], shares=int(h[1]),
                                  available=int(h[2]), cost=h[3])
            for h in self._conn.execute(
                "SELECT symbol, shares, available, cost"
                " FROM acct_snapshot_holdings WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchall()
        }
        return AccountSnapshot(
            snapshot_id=row[0], status=row[1], as_of=row[2],
            captured_at=row[3], source=row[4], import_id=row[5],
            cash_balance=row[6], cash_available=row[7], holdings=holdings,
            note=row[8], raw_json=row[9], content_hash=row[10],
        )

    def _diff_against(self, base: AccountSnapshot, as_of: str,
                      actual_cash: float, actual_holdings: dict,
                      tolerance: float) -> SnapshotDiff:
        proj = self._project(base, as_of)
        cash_diff = None
        delta = actual_cash - proj.cash
        if abs(delta) > tolerance:
            cash_diff = CashDiff(expected=proj.cash, actual=actual_cash,
                                 delta=delta)
        holding_diffs: dict[str, HoldingDiff] = {}
        for symbol in sorted(set(proj.holdings) | set(actual_holdings)):
            expected = proj.holdings[symbol].shares if symbol in proj.holdings else 0
            actual = (actual_holdings[symbol].shares
                      if symbol in actual_holdings else 0)
            if expected != actual:
                holding_diffs[symbol] = HoldingDiff(
                    symbol=symbol, expected=expected, actual=actual,
                    delta=actual - expected)
        return SnapshotDiff(cash=cash_diff, holding_diffs=holding_diffs,
                            tolerance=tolerance)

    def _store_diff_rows(self, snapshot_id: int, diff: SnapshotDiff):
        rows = []
        if diff.cash is not None:
            rows.append((snapshot_id, "cash", "", diff.cash.expected,
                         diff.cash.actual))
        for symbol, d in sorted(diff.holding_diffs.items()):
            rows.append((snapshot_id, "shares", symbol, d.expected, d.actual))
        if rows:
            self._conn.executemany(
                "INSERT INTO acct_pending_diffs (snapshot_id, kind, symbol,"
                " expected, actual) VALUES (?, ?, ?, ?, ?)", rows)

    def _delete_diff_rows(self, snapshot_id: int):
        self._conn.execute(
            "DELETE FROM acct_pending_diffs WHERE snapshot_id = ?",
            (snapshot_id,),
        )

    def _load_diff(self, snapshot_id: int) -> SnapshotDiff:
        cash, holding_diffs = None, {}
        for kind, symbol, expected, actual in self._conn.execute(
                "SELECT kind, symbol, expected, actual FROM acct_pending_diffs"
                " WHERE snapshot_id = ?", (snapshot_id,)).fetchall():
            if kind == "cash":
                cash = CashDiff(expected=expected, actual=actual,
                                delta=actual - expected)
            else:
                holding_diffs[symbol] = HoldingDiff(
                    symbol=symbol, expected=int(expected), actual=int(actual),
                    delta=int(actual - expected))
        return SnapshotDiff(cash=cash, holding_diffs=holding_diffs)

    def _expire_stale_candidates(self, confirmed_as_of: str):
        # 同日候选也无法再确认（基准已到同一天），一并过期
        stale = self._conn.execute(
            "SELECT snapshot_id FROM acct_snapshots WHERE status = ?"
            " AND as_of <= ?", (SNAPSHOT_CANDIDATE, confirmed_as_of),
        ).fetchall()
        for (snapshot_id,) in stale:
            self._conn.execute(
                "UPDATE acct_snapshots SET status = ? WHERE snapshot_id = ?",
                (SNAPSHOT_EXPIRED, snapshot_id),
            )
            self._delete_diff_rows(snapshot_id)
            _insert_audit(self._conn, "snapshot_expired", "snapshot",
                          str(snapshot_id), {"as_of_confirmed": confirmed_as_of})

    # ── 内部：成交入账与修正 ──

    def _insert_fills(self, source: str, normalized: list[dict],
                      batch_info: dict | None, action: str,
                      note: str = "") -> ImportFillsResult:
        source = require_text(source, "source")
        try:
            with self._txn():
                self._require_base("record_fill")
                base = self._base_snapshot()
                skipped = 0
                to_insert = []
                for row in normalized:
                    existing = self._identity_row(
                        "acct_fills", "fill_id", source, row["trade_id"],
                        "date, symbol, side, quantity, price, fee_commission,"
                        " fee_tax, fee_transfer, fee_other, plan_id, order_ref,"
                        " note")
                    if existing is not None:
                        stored = dict(zip(
                            ("date", "symbol", "side", "quantity", "price",
                             "fee_commission", "fee_tax", "fee_transfer",
                             "fee_other", "plan_id", "order_ref", "note"),
                            existing[2:]))
                        if not _same_content(stored, _fill_canonical(row)):
                            raise _content_conflict(
                                "fill", (source, row["trade_id"]), stored,
                                _fill_canonical(row))
                        skipped += 1
                        continue
                    to_insert.append(row)
                if batch_info is not None:
                    batch_row = self._conn.execute(
                        "SELECT content_hash FROM acct_import_batches"
                        " WHERE source = ? AND batch_id = ?",
                        (batch_info["source"], batch_info["batch_id"]),
                    ).fetchone()
                    if batch_row is not None:
                        if batch_row[0] != batch_info["content_hash"]:
                            raise AccountConflictError(
                                f"导入批次 ({batch_info['source']},"
                                f" {batch_info['batch_id']}) 已存在且内容不同"
                                "——同批重入不允许改写内容"
                            )
                        return ImportFillsResult(
                            appended=0, skipped=len(normalized), fills=(),
                            action="idempotent_noop")
                if to_insert:
                    # 写前模拟整批投影：矛盾在此拒绝，内层失败零写入
                    self._prevalidate_fills(to_insert)
                inserted: list[FillRecord] = []
                now = _now()
                for row in to_insert:
                    fill_id = self._next_id("acct_fills", "fill_id")
                    pre_base = row["date"] <= base.as_of
                    self._conn.execute(
                        "INSERT INTO acct_fills (fill_id, source, trade_id, date,"
                        " symbol, side, quantity, price, fee_commission, fee_tax,"
                        " fee_transfer, fee_other, plan_id, order_ref, external,"
                        " note, created_at) VALUES"
                        " (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (fill_id, source, row["trade_id"], row["date"],
                         row["symbol"], row["side"], row["quantity"],
                         row["price"], row["fee_commission"], row["fee_tax"],
                         row["fee_transfer"], row["fee_other"],
                         row["plan_id"], row["order_ref"], row["external"],
                         row["note"], now),
                    )
                    inserted.append(self._load_fill(source, row["trade_id"]))
                    _insert_audit(self._conn, action, "fill",
                                  f"{source}:{row['trade_id']}",
                                  {"date": row["date"], "symbol": row["symbol"],
                                   "side": row["side"], "pre_base": pre_base,
                                   "note": note})
                if batch_info is not None:
                    self._conn.execute(
                        "INSERT INTO acct_import_batches (source, batch_id,"
                        " content_hash, n_rows, created_at)"
                        " VALUES (?, ?, ?, ?, ?)",
                        (batch_info["source"], batch_info["batch_id"],
                         batch_info["content_hash"], batch_info["n_rows"], now),
                    )
                    _insert_audit(self._conn, "fills_batch_imported", "batch",
                                  f"{batch_info['source']}:{batch_info['batch_id']}",
                                  {"appended": len(inserted), "skipped": skipped})
                return ImportFillsResult(
                    appended=len(inserted), skipped=skipped,
                    fills=tuple(inserted), action="imported")
        except LiveAccountError as exc:
            self._audit_rejection(action, exc)
            raise

    def _correct(self, *, target_source, target_trade_id, event_source,
                 event_id, date, kind, fields, note: str) -> FillCorrection:
        self._ensure_writable()
        target_source = require_text(target_source, "target_source")
        target_trade_id = require_text(target_trade_id, "target_trade_id")
        event_source = require_text(event_source, "event_source")
        event_id = require_text(event_id, "event_id")
        date = validate_date_format(date, "date")
        note = optional_text(note, "note")
        if kind == CORRECTION:
            if not isinstance(fields, dict) or not fields:
                raise AccountValidationError("correct_fill 需要非空 fields 修正字段")
            bad = set(fields) - CORRECTION_FIELDS
            if bad:
                raise AccountValidationError(
                    f"修正字段不支持 {sorted(bad)}（允许 {sorted(CORRECTION_FIELDS)}；"
                    "symbol/side 变更请 void 后重新入账）")
            checked = {}
            for key, value in fields.items():
                if key == "date":
                    checked[key] = validate_date_format(value, f"fields.{key}")
                elif key == "price":
                    checked[key] = require_positive_price(value, f"fields.{key}")
                elif key == "quantity":
                    checked[key] = require_whole_int(value, f"fields.{key}", 1)
                else:
                    checked[key] = require_non_negative(value, f"fields.{key}")
            fields_json = json.dumps(
                {k: checked[k] for k in sorted(checked)}, ensure_ascii=False)
        else:
            fields_json = "{}"
        try:
            with self._txn():
                target = self._identity_row(
                    "acct_fills", "fill_id", target_source, target_trade_id,
                    "")
                if target is None:
                    raise AccountStateError(
                        f"目标成交不存在: ({target_source}, {target_trade_id})")
                fill_id = target[0]
                existing = self._identity_row(
                    "acct_fill_corrections", "correction_id", event_source,
                    event_id, "fill_id, kind, date, fields_json")
                if existing is not None:
                    # 行结构: (correction_id, source, fill_id, kind, date, fields_json)
                    if (existing[2] != fill_id or existing[3] != kind
                            or existing[4] != date or existing[5] != fields_json):
                        raise AccountConflictError(
                            f"修正身份 ({event_source}, {event_id}) 已存在且内容不同"
                            "——拒绝覆盖；请使用新的 event_id")
                    return self._load_correction(existing[0])
                self._prevalidate_correction(
                    target_source, target_trade_id, kind,
                    checked if kind == CORRECTION else None)
                correction_id = self._next_id("acct_fill_corrections",
                                              "correction_id")
                now = _now()
                self._conn.execute(
                    "INSERT INTO acct_fill_corrections (correction_id, fill_id,"
                    " kind, date, fields_json, source, event_id, note,"
                    " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (correction_id, fill_id, kind, date, fields_json,
                     event_source, event_id, note, now),
                )
                action = "fill_corrected" if kind == CORRECTION else "fill_voided"
                _insert_audit(self._conn, action, "fill",
                              f"{target_source}:{target_trade_id}",
                              {"correction_id": correction_id, "fields": fields})
                return self._load_correction(correction_id)
        except LiveAccountError as exc:
            self._audit_rejection("fill_correction", exc)
            raise

    def _load_fill(self, source: str, trade_id: str) -> FillRecord:
        row = self._conn.execute(
            f"SELECT {_FILL_COLUMNS} FROM acct_fills"
            " WHERE source = ? AND trade_id = ?", (source, trade_id),
        ).fetchone()
        if row is None:
            raise AccountStateError(f"成交不存在: ({source}, {trade_id})")
        return self._fill_from_row(row)

    @staticmethod
    def _fill_from_row(row) -> FillRecord:
        return FillRecord(**dict(zip(_FILL_KEYS, row)))

    def _load_correction(self, correction_id: int) -> FillCorrection:
        row = self._conn.execute(
            "SELECT c.correction_id, c.fill_id, f.trade_id, c.kind, c.date,"
            " c.fields_json, c.source, c.event_id, c.note, c.created_at"
            " FROM acct_fill_corrections c JOIN acct_fills f"
            " ON f.fill_id = c.fill_id WHERE c.correction_id = ?",
            (correction_id,),
        ).fetchone()
        return FillCorrection(
            correction_id=row[0], fill_id=row[1], target_trade_id=row[2],
            kind=row[3], date=row[4], fields=json.loads(row[5]), source=row[6],
            event_id=row[7], note=row[8], created_at=row[9],
        )

    def _load_cash_flow(self, flow_id: int) -> CashFlow:
        row = self._conn.execute(
            "SELECT flow_id, date, category, amount, symbol, source, event_id,"
            " note, created_at, superseded_by FROM acct_cash_flows"
            " WHERE flow_id = ?", (flow_id,),
        ).fetchone()
        return CashFlow(*row)

    def _load_share_adjustment(self, adj_id: int) -> ShareAdjustment:
        row = self._conn.execute(
            "SELECT adj_id, date, symbol, delta_shares, category, reason,"
            " source, event_id, created_at, superseded_by"
            " FROM acct_share_adjustments WHERE adj_id = ?", (adj_id,),
        ).fetchone()
        return ShareAdjustment(*row)

    # ── 内部：投影引擎（加载与应用分离，支撑写前校验）──

    def _load_facts(self) -> tuple:
        """读取未 superseded 的三类事实；写前校验在此基础上追加模拟行。"""
        fills = self._effective_fills()
        flows = self._conn.execute(
            "SELECT date, amount FROM acct_cash_flows"
            " WHERE superseded_by IS NULL ORDER BY flow_id").fetchall()
        adjustments = self._conn.execute(
            "SELECT date, symbol, delta_shares FROM acct_share_adjustments"
            " WHERE superseded_by IS NULL ORDER BY adj_id").fetchall()
        return fills, flows, adjustments

    def _project(self, base: AccountSnapshot,
                 as_of_limit: str | None = None) -> ProjectedState:
        fills, flows, adjustments = self._load_facts()
        return self._apply_facts(base, fills, flows, adjustments, as_of_limit)

    def _effective_fills(self) -> list[dict]:
        rows = self._conn.execute(
            f"SELECT {_FILL_COLUMNS} FROM acct_fills ORDER BY fill_id"
        ).fetchall()
        corrections: dict[int, list[tuple]] = {}
        for row in self._conn.execute(
                "SELECT fill_id, kind, fields_json FROM acct_fill_corrections"
                " ORDER BY correction_id").fetchall():
            corrections.setdefault(row[0], []).append((row[1], row[2]))
        effective = []
        for row in rows:
            record = dict(zip(_FILL_KEYS, row))
            voided = False
            for kind, fields_json in corrections.get(record["fill_id"], ()):
                if kind == VOID:
                    voided = True
                    break
                record.update(json.loads(fields_json))
            if voided or record["superseded_by"] is not None:
                continue
            effective.append(record)
        effective.sort(key=lambda r: (r["date"], r["fill_id"]))
        return effective

    @staticmethod
    def _apply_facts(base: AccountSnapshot, fills: list, flows: list,
                     adjustments: list,
                     as_of_limit: str | None = None) -> ProjectedState:
        ordered_fills = sorted(fills, key=lambda r: (r["date"], r["fill_id"]))
        cash = base.cash_balance
        holdings: dict[str, ProjectedHolding] = {}
        for symbol, h in base.holdings.items():
            holdings[symbol] = ProjectedHolding(
                symbol=symbol, shares=h.shares,
                cost_amount=(h.cost or 0.0) * h.shares,
                cost_known=h.cost is not None)

        def in_window(date: str) -> bool:
            if date <= base.as_of:
                return False
            return as_of_limit is None or date <= as_of_limit

        pre_base = 0
        projected_through = base.as_of

        for fill in ordered_fills:
            if not in_window(fill["date"]):
                if fill["date"] <= base.as_of:
                    pre_base += 1
                continue
            symbol = fill["symbol"]
            quantity = int(fill["quantity"])
            price = float(fill["price"])
            fees = (float(fill["fee_commission"]) + float(fill["fee_tax"])
                    + float(fill["fee_transfer"]) + float(fill["fee_other"]))
            if fill["side"] == SIDE_BUY:
                cash -= quantity * price + fees
                holding = holdings.get(symbol)
                if holding is None:
                    holdings[symbol] = ProjectedHolding(
                        symbol=symbol, shares=quantity,
                        cost_amount=quantity * price + fees, cost_known=True)
                else:
                    holding.shares += quantity
                    holding.cost_amount += quantity * price + fees
                    holding.cost_known = True
                if cash < -1e-9:
                    # 仅提示事实不完整，不构成可交易状态；预算层必须拦截
                    logger.warning(
                        "投影现金为负 %.2f（%s BUY %s）——事实不完整（缺入金/"
                        "费用记录？），不可作为可交易状态，预算层必须拦截",
                        cash, fill["date"], symbol)
            else:
                holding = holdings.get(symbol)
                held = holding.shares if holding else 0
                if held < quantity:
                    raise AccountStateError(
                        f"投影不一致: {fill['date']} SELL {symbol} {quantity} 股"
                        f"超过投影持仓 {held} 股——账本事实矛盾，请修正后重放")
                cash += quantity * price - fees
                if holding.cost_known and holding.shares > 0:
                    holding.cost_amount -= (
                        holding.cost_amount * quantity / holding.shares)
                holding.shares -= quantity
                if holding.shares == 0:
                    del holdings[symbol]
            projected_through = max(projected_through, fill["date"])

        for flow in flows:
            if not in_window(flow[0]):
                if flow[0] <= base.as_of:
                    pre_base += 1
                continue
            cash += float(flow[1])
            projected_through = max(projected_through, flow[0])

        for adj in adjustments:
            if not in_window(adj[0]):
                if adj[0] <= base.as_of:
                    pre_base += 1
                continue
            symbol, delta = adj[1], int(adj[2])
            holding = holdings.get(symbol)
            new_shares = (holding.shares if holding else 0) + delta
            if new_shares < 0:
                raise AccountStateError(
                    f"投影不一致: {adj[0]} {symbol} 股数调整 {delta:+d} 后为负"
                    f"（{new_shares}）——账本事实矛盾")
            if new_shares == 0:
                holdings.pop(symbol, None)
            elif holding is None:
                holdings[symbol] = ProjectedHolding(
                    symbol=symbol, shares=new_shares, cost_known=False)
            else:
                holding.shares = new_shares
            projected_through = max(projected_through, adj[0])

        return ProjectedState(
            base_snapshot_id=base.snapshot_id, base_as_of=base.as_of,
            cash=cash, cash_available=base.cash_available + (cash - base.cash_balance),
            holdings=holdings, projected_through=projected_through,
            pre_base_fact_count=pre_base,
        )

    # ── 写前校验：在任何 INSERT 之前模拟投影，内层失败零写入（§3.2）──

    def _prevalidate_fills(self, new_rows: list[dict]):
        base = self._require_base("projection")
        fills, flows, adjustments = self._load_facts()
        next_id = max((f["fill_id"] for f in fills), default=0) + 1
        simulated = fills + [
            dict(row, fill_id=next_id + offset)
            for offset, row in enumerate(new_rows)
        ]
        self._apply_facts(base, simulated, flows, adjustments)

    def _prevalidate_adjustment(self, date: str, symbol: str, delta: int):
        base = self._require_base("projection")
        fills, flows, adjustments = self._load_facts()
        self._apply_facts(base, fills, flows, adjustments + [(date, symbol, delta)])

    def _prevalidate_correction(self, target_source: str, target_trade_id: str,
                                kind: str, fields: dict | None):
        """模拟修正/作废后的投影；kind=VOID 时 fields 传 None 表示剔除该笔。"""
        base = self._require_base("projection")
        fills, flows, adjustments = self._load_facts()
        simulated = []
        for fill in fills:
            if fill["source"] == target_source and fill["trade_id"] == target_trade_id:
                if kind == VOID:
                    continue
                patched = dict(fill)
                patched.update(fields)
                simulated.append(patched)
            else:
                simulated.append(fill)
        self._apply_facts(base, simulated, flows, adjustments)

    def _require_base(self, operation: str) -> AccountSnapshot:
        base = self._base_snapshot()
        if base is None:
            raise AccountStateError(
                f"{operation} 需要已确认的账户快照基准——请先 import_snapshot"
                " 并确认（首次导入自动确认为期初）")
        return base

    # ── 内部：权益与 PnL 辅助 ──

    def _equity_of(self, proj: ProjectedState, prices) -> EquityView:
        if not proj.holdings:
            return EquityView(cash=proj.cash, holdings_value=0.0,
                              total=proj.cash, missing_prices=())
        missing = []
        if prices is None:
            missing = sorted(proj.holdings)
        else:
            for symbol in sorted(proj.holdings):
                if symbol not in prices:
                    missing.append(symbol)
        if missing:
            return EquityView(cash=proj.cash, holdings_value=None, total=None,
                              missing_prices=tuple(missing))
        holdings_value = 0.0
        for symbol, holding in proj.holdings.items():
            price = require_positive_price(prices[symbol], f"prices[{symbol}]")
            holdings_value += holding.shares * price
        return EquityView(cash=proj.cash, holdings_value=holdings_value,
                          total=proj.cash + holdings_value, missing_prices=())

    @staticmethod
    def _value_holdings(holdings: dict, prices, reason_prefix: str,
                        reasons: list) -> float | None:
        if prices is None:
            reasons.extend(f"{reason_prefix}:{s}" for s in sorted(holdings))
            return None
        missing = [s for s in sorted(holdings) if s not in prices]
        if missing:
            reasons.extend(f"{reason_prefix}:{s}" for s in missing)
            return None
        return sum(holdings[s].shares
                   * require_positive_price(prices[s], f"prices[{s}]")
                   for s in holdings)

    # ── 内部：通用工具 ──

    def _next_id(self, table: str, column: str) -> int:
        row = self._conn.execute(
            f"SELECT COALESCE(MAX({column}), 0) + 1 FROM {table}").fetchone()
        return int(row[0])

    def _count(self, table: str) -> int:
        return int(self._conn.execute(
            f"SELECT COUNT(*) FROM {table}").fetchone()[0])

    def _identity_row(self, table: str, id_col: str, source: str,
                      event_id: str, extra_columns: str):
        sql = f"SELECT {id_col}, source"
        if extra_columns:
            sql += f", {extra_columns}"
        sql += f" FROM {table} WHERE source = ? AND "
        key_col = "trade_id" if table == "acct_fills" else "event_id"
        sql += f"{key_col} = ?"
        return self._conn.execute(sql, (source, event_id)).fetchone()

    def _audit_rejection(self, action: str, exc: LiveAccountError):
        """拒绝审计：保留可诊断输入，不污染事实表（§3.2）。

        仅在独立失败（无存活外层事务）时持久化；组合事务内的失败必然
        rollback-only，审计写了也会随整体回滚丢弃，故直接跳过，
        诊断信息由异常本身携带上抛。
        """
        if self.read_only or self._txn_depth > 0:
            return
        try:
            with self._txn():
                _insert_audit(self._conn, "input_rejected", "action", action, {
                    "error_code": exc.code, "message": str(exc)[:2000],
                })
        except duckdb.Error:
            logger.warning("拒绝审计写入失败: %s", action)


def _insert_audit(conn, action: str, subject_type: str, subject_id: str,
                  detail: dict):
    seq = conn.execute(
        "SELECT COALESCE(MAX(event_seq), 0) + 1 FROM acct_audit").fetchone()[0]
    conn.execute(
        "INSERT INTO acct_audit (event_seq, event_at, actor, action, subject_type,"
        " subject_id, detail_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (int(seq), _now(), "user", action, subject_type, str(subject_id),
         json.dumps(detail, ensure_ascii=False, sort_keys=True, default=str)),
    )


def _parse_cash_input(cash, cash_balance, cash_available) -> tuple[float, float]:
    """资金输入：cash 简化输入按 余额=可用、冻结=0；与显式双值互斥。"""
    if cash is not None:
        if cash_balance is not None or cash_available is not None:
            raise AccountValidationError(
                "cash 简化输入与 cash_balance/cash_available 互斥——"
                "简化输入按 余额=可用现金、冻结=0 解释")
        balance = require_non_negative(cash, "cash")
        return balance, balance
    if cash_balance is None or cash_available is None:
        raise AccountValidationError(
            "资金输入必须提供 cash（简化）或 cash_balance+cash_available（完整）")
    balance = require_non_negative(cash_balance, "cash_balance")
    available = require_non_negative(cash_available, "cash_available")
    if available > balance:
        raise AccountValidationError(
            f"cash_available({available}) 大于 cash_balance({balance})"
            "——冻结资金不能为负")
    return balance, available


def _normalize_holdings(holdings, context: str) -> dict:
    """全量持仓校验：缺 holdings 拒绝；空仓必须显式空列表（§3.1）。"""
    if holdings is None:
        raise AccountValidationError(
            f"{context}: holdings 缺失——缺 holdings 不等于空仓，"
            "合法空仓需显式传入空列表")
    if not isinstance(holdings, (list, tuple)):
        raise AccountValidationError(
            f"{context}: holdings 必须为列表，得到 {type(holdings).__name__}")
    out: dict[str, SnapshotHolding] = {}
    for index, row in enumerate(holdings, start=1):
        prefix = f"{context} holdings 第 {index} 行"
        if isinstance(row, SnapshotHolding):
            symbol = require_text(row.symbol, f"{prefix} symbol")
            shares = require_whole_int(row.shares, f"{prefix} shares", 1)
            available = require_whole_int(row.available, f"{prefix} available", 0)
            cost = row.cost
        elif isinstance(row, dict):
            symbol = require_text(row.get("symbol"), f"{prefix} symbol")
            shares = require_whole_int(row.get("shares"), f"{prefix} shares", 1)
            raw_available = row.get("available")
            available = (require_whole_int(raw_available, f"{prefix} available", 0)
                         if raw_available is not None else shares)
            cost = row.get("cost")
        else:
            raise AccountValidationError(f"{prefix}: 非法条目 {row!r}")
        if cost is not None:
            cost = require_non_negative(cost, f"{prefix} cost")
        if available > shares:
            raise AccountValidationError(
                f"{prefix}: available({available}) 大于 shares({shares})")
        if symbol in out:
            raise AccountValidationError(f"{prefix}: 重复 symbol {symbol}")
        out[symbol] = SnapshotHolding(symbol=symbol, shares=shares,
                                      available=available, cost=cost)
    return out


def _normalize_fill_row(row: dict, context: str) -> dict:
    """单行成交规范化校验；返回带稳定身份的规范 dict。"""
    date = validate_date_format(row.get("date"), f"{context} date")
    symbol = require_text(row.get("symbol"), f"{context} symbol")
    side = str(row.get("side", "")).strip().upper()
    if side not in SIDES:
        raise AccountValidationError(
            f"{context} side 非法: {row.get('side')!r}（允许 {sorted(SIDES)}）")
    quantity = require_whole_int(row.get("quantity"), f"{context} quantity", 1)
    price = require_positive_price(row.get("price"), f"{context} price")
    fees = {}
    for key in ("fee_commission", "fee_tax", "fee_transfer", "fee_other"):
        raw_fee = row.get(key)
        fees[key] = require_non_negative(
            0.0 if raw_fee is None else raw_fee, f"{context} {key}")
    trade_id = require_text(row.get("trade_id"), f"{context} trade_id")
    plan_id = optional_text(row.get("plan_id"), f"{context} plan_id")
    order_ref = optional_text(row.get("order_ref"), f"{context} order_ref")
    note = optional_text(row.get("note"), f"{context} note")
    external = row.get("external")
    if external is None:
        external = plan_id == ""  # 不关联计划的成交按 external/manual 标记
    if not isinstance(external, bool):
        raise AccountValidationError(f"{context} external 必须为布尔: {external!r}")
    return {
        "date": date, "symbol": symbol, "side": side, "quantity": quantity,
        "price": price, **fees, "trade_id": trade_id, "plan_id": plan_id,
        "order_ref": order_ref, "note": note, "external": external,
    }


def _fill_canonical(row: dict) -> dict:
    return {
        "trade_id": row["trade_id"], "date": row["date"],
        "symbol": row["symbol"], "side": row["side"],
        "quantity": row["quantity"], "price": _round6(row["price"]),
        "fee_commission": _round6(row["fee_commission"]),
        "fee_tax": _round6(row["fee_tax"]),
        "fee_transfer": _round6(row["fee_transfer"]),
        "fee_other": _round6(row["fee_other"]), "plan_id": row["plan_id"],
        "order_ref": row["order_ref"], "note": row["note"],
    }
