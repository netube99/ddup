"""实盘 V2 策略会话层 — docs/live_redesign.md §4。

职责（策略会话与决策）：
- 启用记录（activation）：effective_date、策略身份（源码/配置/模型哈希）、
  接管政策与初始锚点；正式版本固定，换策略必须新 activation，不能用新
  策略重写旧决策；试算（trial）不改变正式会话。
- 实际账户桥：账本投影 → 引擎账户；observe/无锚点持仓不进入策略可见
  账户（不编造 entry_price/entry_date，A01），但仍计资产与资金占用（A12）。
  已显式接管确认的快照可以继续交易；takeover_gap 只留在 PnL incomplete。
- 因果回放与只读预览：从 activation 边界按已记录的实际账户事实（成交/
  现金流/股数调整）确定性重放策略钩子，产出到信号日的决策；预览不写
  任何表、不推进正式计数。恢复＝重放：重启后结果与连续运行一致
  （A15/A16），不 pickle 策略对象、不重跑启用前账户生命史。
- 接受/跳过：接受在单一组合事务内落计划 + 决策日志（幂等 + 版本检查）；
  计划接受绝不提前记成交。

边界：依赖账户/计划模块与 btcore 引擎决策面（Engine.prepare /
compute_pending 为回测/实盘回放共用入口）；不修改用户策略、不连接
真实券商。回放成本 O(启用以来交易日 × 策略 select 成本)，限制与
成本记录于 docs/live_guide（S3 维护）。
"""

import hashlib
import inspect
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from btcore.corporate import apply_condition_rescale
from btcore.engine import Engine, bars_to_dict, value_account
from btcore.provider import DataProvider
from btcore.types import Account, Holding, Trade
from research.live_account import (
    POLICY_MANAGED,
    POLICY_OBSERVE,
    AccountLedger,
    LiveAccountError,
    require_positive_price,
    require_text,
    validate_date_format,
)
from research.live_plan import (
    AccountView,
    ExecutionConfig,
    HoldingView,
    PlanStateError,
    PlanStore,
    build_plan,
)

logger = logging.getLogger(__name__)

SESS_SCHEMA_VERSION = 1

ACTIVATION_ACTIVE = "active"
ACTIVATION_SUPERSEDED = "superseded"

FAR_END = "99991231"


# ── 异常 ──


class SessionError(LiveAccountError):
    code = "session"


class SessionValidationError(SessionError):
    code = "validation"


class SessionStateError(SessionError):
    code = "state"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def _canonical_json(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)


# ── 记录类型 ──


@dataclass(frozen=True)
class StrategyIdentity:
    """策略正式版本身份：源码哈希 + 配置哈希 + 模型声明。"""

    name: str
    source_kind: str  # class | yaml
    source_path: str
    code_hash: str
    config: dict
    config_hash: str
    models: tuple = ()  # 每项为模型 run_summary dict

    def matches(self, other: "StrategyIdentity") -> bool:
        """身份一致判定：name+config 必须一致；code_hash 只在同源类型间比较
        （YAML 启用与类模块哈希来源不同，不要求相等）。"""
        if self.name != other.name or self.config_hash != other.config_hash:
            return False
        if (self.source_kind == other.source_kind and self.code_hash
                and other.code_hash):
            return self.code_hash == other.code_hash
        return True


@dataclass(frozen=True)
class ActivationRecord:
    """一次策略启用：生效日、身份、接管锚点与状态。"""

    activation_id: int
    effective_date: str
    identity: StrategyIdentity
    anchors: dict  # {symbol: {"entry_date": ..., "entry_price": ...}}
    status: str
    note: str
    created_at: str
    superseded_by: int | None = None


@dataclass(frozen=True)
class DecisionView:
    """只读预览产物：决策输入版本、动作、订单草稿与警告。

    is_trial=True 的视图不可接受（试算不改正式会话）。
    """

    activation_id: int
    snapshot_id: int
    signal_date: str
    trade_date: str
    expiry_date: str
    decision_hash: str
    actions: dict
    holding_conditions: dict
    orders: object  # PlanDraft
    warnings: tuple
    replay_days: int
    visible_holdings: tuple
    total_value: float = 0.0
    is_trial: bool = False


# ── schema ──

_SESS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sess_meta (
    key   VARCHAR PRIMARY KEY,
    value VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS sess_activations (
    activation_id  INTEGER PRIMARY KEY,
    effective_date VARCHAR NOT NULL,
    strategy_name  VARCHAR NOT NULL,
    source_kind    VARCHAR NOT NULL,
    source_path    VARCHAR NOT NULL DEFAULT '',
    code_hash      VARCHAR NOT NULL DEFAULT '',
    config_json    VARCHAR NOT NULL,
    config_hash    VARCHAR NOT NULL,
    models_json    VARCHAR NOT NULL DEFAULT '[]',
    anchors_json   VARCHAR NOT NULL DEFAULT '{}',
    status         VARCHAR NOT NULL,
    note           VARCHAR NOT NULL DEFAULT '',
    created_at     VARCHAR NOT NULL,
    superseded_by  INTEGER
);

CREATE TABLE IF NOT EXISTS sess_decisions (
    decision_id   INTEGER PRIMARY KEY,
    activation_id INTEGER NOT NULL,
    signal_date   VARCHAR NOT NULL,
    snapshot_id   INTEGER NOT NULL,
    decision_hash VARCHAR NOT NULL,
    actions_json  VARCHAR NOT NULL,
    plan_id       VARCHAR NOT NULL DEFAULT '',
    event         VARCHAR NOT NULL,
    created_at    VARCHAR NOT NULL
);
"""


@dataclass
class _ReplayResult:
    actions: dict
    conditions: dict
    ref_prices: dict
    total_value: float
    days: int
    warnings: list


class StrategySession:
    """策略会话：启用、预览、接受/跳过的编排与回放恢复。

    用法：``StrategySession.open(ledger, provider, strategy=inst)``；
    strategy 是可选的运行时句柄（YAML 启用在重启后可按 source_path
    重建，无需句柄）。所有预览只读；写操作只有 activate/accept/skip
    及委托 PlanStore 的执行回报。
    """

    def __init__(self, ledger: AccountLedger, provider: DataProvider,
                 strategy=None):
        self.ledger = ledger
        self.provider = provider
        self._strategy = strategy
        self.plans = PlanStore(ledger)

    @classmethod
    def open(cls, ledger: AccountLedger, provider: DataProvider, *,
             strategy=None) -> "StrategySession":
        if ledger.read_only:
            tables = {row[0] for row in ledger.connection.execute(
                "SELECT table_name FROM information_schema.tables").fetchall()}
            if "sess_activations" not in tables:
                raise SessionStateError(
                    "只读连接且会话表不存在——先用可写连接 open 一次以建表")
            return cls(ledger, provider, strategy)
        session = cls(ledger, provider, strategy)
        with ledger.transaction():
            ledger.connection.execute(_SESS_SCHEMA_SQL)
            row = ledger.connection.execute(
                "SELECT value FROM sess_meta WHERE key = 'schema_version'"
            ).fetchone()
            version = row[0] if row else None
            if version is None:
                ledger.connection.execute(
                    "INSERT INTO sess_meta (key, value)"
                    " VALUES ('schema_version', ?)", (str(SESS_SCHEMA_VERSION),))
            elif version != str(SESS_SCHEMA_VERSION):
                raise SessionStateError(
                    f"会话表 schema_version={version} 不受支持"
                    f"（当前 {SESS_SCHEMA_VERSION}）")
            session.plans = PlanStore.attach(ledger)
        return session

    # ── 启用（§4：启用日期/版本/接管政策/初始锚点） ──

    def activate(self, *, effective_date: str, strategy=None,
                 yaml_path: str = "", anchors: dict | None = None,
                 manage_anchored: bool = True, note: str = "") -> ActivationRecord:
        """启用策略：记录生效日、版本身份与接管锚点。

        - 新 activation 显式 supersede 当前 activation；effective_date
          不得早于前一启用（不重写启用前历史）。
        - anchors: {symbol: {"entry_date", "entry_price"}}，symbol 必须在
          当前投影持仓中；缺锚点的持仓保持 observe（策略不可见，不编造）。
        - manage_anchored=True 时为锚点持仓登记 managed 政策（S1 公共
          API set_holding_policy），使策略可交易这些持仓。
        """
        effective_date = validate_date_format(effective_date, "effective_date")
        strategy = strategy if strategy is not None else self._strategy
        if strategy is None:
            raise SessionValidationError(
                "activate 需要 strategy 实例（或先在 open 时 attach）")
        identity = self._identity_of(strategy, yaml_path)
        with self.ledger.transaction():
            confirmed = self.ledger.confirmed_snapshot()
            if confirmed is None:
                raise SessionStateError(
                    "无确认快照——先导入/确认账户快照再启用策略")
            if effective_date < confirmed.as_of:
                raise SessionValidationError(
                    f"effective_date {effective_date} 早于确认基准 "
                    f"{confirmed.as_of}——启用必须不早于账户基准")
            current = self.active_activation()
            if current is not None and effective_date < current.effective_date:
                raise SessionValidationError(
                    f"effective_date {effective_date} 早于当前启用 "
                    f"{current.effective_date}——不能倒退重写启用前历史")
            anchors = self._validate_anchors(anchors or {}, effective_date)
            if manage_anchored:
                for symbol, anchor in sorted(anchors.items()):
                    self.ledger.set_holding_policy(
                        symbol, POLICY_MANAGED,
                        reason=f"activation anchor ({identity.name})",
                        anchor_date=anchor["entry_date"],
                        anchor_price=anchor["entry_price"])
            activation_id = self._next_activation_id()
            now = _now()
            if current is not None:
                self.ledger.connection.execute(
                    "UPDATE sess_activations SET status = ?, superseded_by = ?"
                    " WHERE activation_id = ?",
                    (ACTIVATION_SUPERSEDED, activation_id,
                     current.activation_id))
            self.ledger.connection.execute(
                "INSERT INTO sess_activations (activation_id, effective_date,"
                " strategy_name, source_kind, source_path, code_hash,"
                " config_json, config_hash, models_json, anchors_json, status,"
                " note, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (activation_id, effective_date, identity.name,
                 identity.source_kind, identity.source_path, identity.code_hash,
                 _canonical_json(identity.config), identity.config_hash,
                 _canonical_json(list(identity.models)),
                 _canonical_json(anchors), ACTIVATION_ACTIVE, note, now))
            record = self._load_activation(activation_id)
        self._strategy = strategy
        return record

    def _validate_anchors(self, anchors: dict, effective_date: str) -> dict:
        if not isinstance(anchors, dict):
            raise SessionValidationError(
                f"anchors 必须是 {{symbol: {{entry_date, entry_price}}}}: "
                f"{anchors!r}")
        projection = self.ledger.projection()
        normalized = {}
        for symbol, anchor in anchors.items():
            symbol = require_text(symbol, "anchor symbol")
            if not isinstance(anchor, dict):
                raise SessionValidationError(
                    f"anchors[{symbol}] 必须是 dict: {anchor!r}")
            entry_date = validate_date_format(
                anchor.get("entry_date"), f"anchors[{symbol}].entry_date")
            entry_price = require_positive_price(
                anchor.get("entry_price"), f"anchors[{symbol}].entry_price")
            if entry_date > effective_date:
                raise SessionValidationError(
                    f"anchors[{symbol}].entry_date {entry_date} 晚于启用日 "
                    f"{effective_date}——锚点必须不晚于启用")
            if symbol not in projection.holdings:
                raise SessionValidationError(
                    f"anchors 含未持仓标的 {symbol}（当前投影持仓: "
                    f"{sorted(projection.holdings)}）——锚点只描述既有持仓")
            normalized[symbol] = {"entry_date": entry_date,
                                  "entry_price": entry_price}
        return normalized

    def _next_activation_id(self) -> int:
        row = self.ledger.connection.execute(
            "SELECT COALESCE(MAX(activation_id), 0) + 1 FROM sess_activations"
        ).fetchone()
        return int(row[0])

    def active_activation(self) -> ActivationRecord | None:
        row = self.ledger.connection.execute(
            "SELECT activation_id FROM sess_activations WHERE status = ?"
            " ORDER BY activation_id DESC LIMIT 1", (ACTIVATION_ACTIVE,),
        ).fetchone()
        return self._load_activation(row[0]) if row else None

    def activations(self) -> list[ActivationRecord]:
        ids = [r[0] for r in self.ledger.connection.execute(
            "SELECT activation_id FROM sess_activations"
            " ORDER BY activation_id").fetchall()]
        return [self._load_activation(i) for i in ids]

    def _load_activation(self, activation_id: int) -> ActivationRecord:
        row = self.ledger.connection.execute(
            "SELECT activation_id, effective_date, strategy_name, source_kind,"
            " source_path, code_hash, config_json, config_hash, models_json,"
            " anchors_json, status, note, created_at, superseded_by"
            " FROM sess_activations WHERE activation_id = ?",
            (activation_id,)).fetchone()
        if row is None:
            raise SessionStateError(f"启用记录不存在: {activation_id}")
        identity = StrategyIdentity(
            name=row[2], source_kind=row[3], source_path=row[4],
            code_hash=row[5], config=json.loads(row[6]), config_hash=row[7],
            models=tuple(json.loads(row[8])))
        return ActivationRecord(
            activation_id=row[0], effective_date=row[1], identity=identity,
            anchors=json.loads(row[9]), status=row[10], note=row[11],
            created_at=row[12], superseded_by=row[13])

    # ── 策略身份与实例化 ──

    def _identity_of(self, strategy, yaml_path: str = "") -> StrategyIdentity:
        cls = type(strategy)
        name = cls.__name__
        config = dict(getattr(strategy, "config", {}) or {})
        if yaml_path:
            source_kind, source_path = "yaml", yaml_path
        else:
            source_kind = "class"
            source_path = inspect.getsourcefile(cls) or ""
        code_hash = self._file_hash(source_path)
        models = []
        for spec in getattr(strategy, "MODEL_SPECS", None) or []:
            try:
                models.append(spec.run_summary())
            except Exception:  # noqa: BLE001 - 鸭子类型模型声明
                models.append({"name": getattr(spec, "name", str(spec))})
        return StrategyIdentity(
            name=name, source_kind=source_kind, source_path=source_path,
            code_hash=code_hash, config=config,
            config_hash=_sha1(_canonical_json(config)), models=tuple(models))

    @staticmethod
    def _file_hash(path: str) -> str:
        if not path:
            return ""
        try:
            with open(path, "rb") as handle:
                return hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ""

    def _materialize_strategy(self, identity: StrategyIdentity):
        """为回放构建全新策略实例（预览可重复、重启可恢复、不共享状态）。

        版本校验：运行时句柄/YAML 文件与启用记录的身份不一致时拒绝
        （正式版本固定，换策略须新 activation）。
        """
        if self._strategy is not None:
            current = self._identity_of(self._strategy)
            if not identity.matches(current):
                raise SessionStateError(
                    f"策略版本与启用记录不一致（启用 {identity.name}/"
                    f"{identity.config_hash[:12]}…，当前 {current.name}/"
                    f"{current.config_hash[:12]}…）——请用新 activation"
                    " 更换策略，不能悄悄替换正式版本")
            return self._clone_strategy(self._strategy, identity.config)
        if identity.source_kind == "yaml":
            from btcore.strategy_loader import load_strategy
            if self._file_hash(identity.source_path) != identity.code_hash:
                raise SessionStateError(
                    f"策略文件 {identity.source_path} 与启用记录的版本不一致"
                    "——请用新 activation 固定新版本")
            loaded = load_strategy(identity.source_path)
            if _sha1(_canonical_json(dict(loaded.config))) != identity.config_hash:
                raise SessionStateError(
                    f"策略配置与启用记录不一致（{identity.source_path}）"
                    "——请用新 activation 固定新配置")
            return loaded
        raise SessionStateError(
            "无运行时策略句柄且启用记录非 YAML——重启后请在 open 时重新 "
            "attach 策略实例")

    @staticmethod
    def _clone_strategy(source, config: dict):
        cls = type(source)
        clone = cls(
            config=dict(config),
            factor_specs=list(getattr(source, "FACTOR_SPECS", None) or []),
            filter_rules=dict(getattr(source, "FILTER_RULES", None) or {}))
        nodes = getattr(source, "FACTOR_NODES", None)
        if nodes is not None:
            clone.FACTOR_NODES = dict(nodes)
        specs = getattr(source, "MODEL_SPECS", None) or []
        if specs:
            clone.MODEL_SPECS = list(specs)
            from btcore.ml import conditions as ml_conditions
            ml_conditions.register()
        return clone

    # ── 预览（只读） ──

    def compute_preview(self, *, signal_date: str | None = None,
                        trade_date: str | None = None,
                        expiry_date: str | None = None,
                        execution: ExecutionConfig | None = None) -> DecisionView:
        """当前启用策略的只读预览：不写任何表、不推进正式计数。"""
        activation = self.active_activation()
        if activation is None:
            raise SessionStateError("无启用策略——先 activate")
        return self._decide(activation, signal_date=signal_date,
                            trade_date=trade_date, expiry_date=expiry_date,
                            execution=execution)

    def compute_trial(self, strategy, *, effective_date: str | None = None,
                      signal_date: str | None = None,
                      trade_date: str | None = None,
                      expiry_date: str | None = None,
                      anchors: dict | None = None,
                      execution: ExecutionConfig | None = None) -> DecisionView:
        """试算另一策略：与正式会话同边界回放，但不写库、不可接受。"""
        activation = self.active_activation()
        if activation is None and effective_date is None:
            raise SessionStateError("无启用策略且未指定 effective_date——"
                                    "试算需要回放边界")
        if effective_date is None:
            effective_date = activation.effective_date
        effective_date = validate_date_format(effective_date, "effective_date")
        identity = self._identity_of(strategy)
        trial_anchors = anchors if anchors is not None else (
            activation.anchors if activation else {})
        trial = ActivationRecord(
            activation_id=0, effective_date=effective_date, identity=identity,
            anchors=dict(trial_anchors), status="trial", note="trial",
            created_at="")
        # 试算策略可能是任意实例：临时挂载计算后恢复，不改变正式会话
        saved = self._strategy
        self._strategy = strategy
        try:
            return self._decide(trial, signal_date=signal_date,
                                trade_date=trade_date,
                                expiry_date=expiry_date, execution=execution,
                                is_trial=True)
        finally:
            self._strategy = saved

    def _decide(self, activation: ActivationRecord, *, signal_date,
                trade_date, expiry_date, execution, is_trial: bool = False) \
            -> DecisionView:
        strategy = self._materialize_strategy(activation.identity)
        confirmed = self.ledger.confirmed_snapshot()
        if confirmed is None:
            raise SessionStateError("无确认快照——账户基准缺失")
        signal = signal_date or self._latest_signal_date(activation)
        signal = validate_date_format(signal, "signal_date")
        trade = trade_date or self._next_trading_day(signal)
        trade = validate_date_format(trade, "trade_date")
        expiry = expiry_date or trade
        expiry = validate_date_format(expiry, "expiry_date")
        if trade < signal:
            raise SessionValidationError(
                f"trade_date {trade} 早于 signal_date {signal}")
        replay = self._replay_to(strategy, activation, signal)
        budget = self.plans.budget_context()
        account_view = self._account_view(confirmed.snapshot_id, signal,
                                          replay.ref_prices)
        conditions = {
            symbol: [dict(c) for c in holding.conditions]
            for symbol, holding in replay.conditions.items()
        }
        if execution is None:
            execution = ExecutionConfig(
                max_positions=int(activation.identity.config.get(
                    "max_positions", 20) or 20))
        draft = build_plan(replay.actions, conditions, account_view, budget,
                           execution)
        decision_hash = _sha1(_canonical_json({
            "trial": is_trial, "activation": activation.activation_id,
            "code_hash": activation.identity.code_hash,
            "config_hash": activation.identity.config_hash,
            "snapshot": confirmed.snapshot_id, "signal": signal,
            "trade": trade, "actions": replay.actions,
            "conditions": conditions, "orders": draft.fingerprint(),
        }))
        warnings = tuple(replay.warnings) + tuple(draft.warnings)
        visible = tuple(sorted(
            (s, h.shares) for s, h in replay.conditions.items()))
        return DecisionView(
            activation_id=activation.activation_id,
            snapshot_id=confirmed.snapshot_id, signal_date=signal,
            trade_date=trade, expiry_date=expiry,
            decision_hash=decision_hash, actions=dict(replay.actions),
            holding_conditions=conditions, orders=draft, warnings=warnings,
            replay_days=replay.days, visible_holdings=visible,
            total_value=account_view.total_value, is_trial=is_trial)

    def _latest_signal_date(self, activation: ActivationRecord) -> str:
        calendar = self.provider.get_calendar(activation.effective_date, FAR_END)
        for day in reversed(calendar):
            frame = self.provider.backend.query_bars(None, day, day)
            if frame is not None and not frame.empty:
                return day
        raise SessionStateError(
            f"启用日 {activation.effective_date} 之后无任何行情日——"
            "请显式指定 signal_date 或更新行情")

    def _next_trading_day(self, day: str) -> str:
        calendar = self.provider.get_calendar(day, FAR_END)
        for candidate in calendar:
            if candidate > day:
                return candidate
        raise SessionStateError(
            f"日历中无晚于 {day} 的交易日（行情未覆盖执行日）——"
            "请显式指定 trade_date")

    # ── 接受 / 跳过（§4：一次性记录决策输入版本、输出与状态提交） ──

    def accept(self, preview: DecisionView, *, actor: str = "user"):
        """接受预览：计划落库 + 决策日志同一组合事务（幂等 + 版本检查）。"""
        if preview.is_trial:
            raise SessionStateError("试算视图不可接受（试算不改正式会话）")
        activation = self.active_activation()
        if activation is None or activation.activation_id != preview.activation_id:
            raise PlanStateError(
                f"预览基于 activation {preview.activation_id}，当前启用为 "
                f"{activation.activation_id if activation else '无'}"
                "——策略已更换，请重新预览")
        self._materialize_strategy(activation.identity)  # 版本漂移校验
        with self.ledger.transaction():
            plan = self.plans.accept_plan(
                preview.orders, activation_id=preview.activation_id,
                snapshot_id=preview.snapshot_id,
                signal_date=preview.signal_date, trade_date=preview.trade_date,
                expiry_date=preview.expiry_date,
                decision_hash=preview.decision_hash, actor=actor)
            self._record_decision(preview, event="accepted",
                                  plan_id=plan.plan_id)
        return plan

    def skip(self, preview: DecisionView, *, reason: str, actor: str = "user"):
        """跳过预览（原因必填）：只记审计，不生成订单、不占预算。"""
        if preview.is_trial:
            raise SessionStateError("试算视图无需跳过（未进入正式会话）")
        activation = self.active_activation()
        if activation is None or activation.activation_id != preview.activation_id:
            raise PlanStateError(
                f"预览基于 activation {preview.activation_id}，当前启用为 "
                f"{activation.activation_id if activation else '无'}")
        with self.ledger.transaction():
            plan = self.plans.skip_decision(
                activation_id=preview.activation_id,
                snapshot_id=preview.snapshot_id,
                signal_date=preview.signal_date,
                decision_hash=preview.decision_hash, reason=reason,
                trade_date=preview.trade_date, actor=actor)
            self._record_decision(preview, event="skipped",
                                  plan_id=plan.plan_id)
        return plan

    def expire_plans(self, as_of: str, *, actor: str = "user"):
        """过期未执行计划（委托 PlanStore）。"""
        return self.plans.expire_plans(as_of, actor=actor)

    def _record_decision(self, preview: DecisionView, *, event: str,
                         plan_id: str) -> None:
        existing = self.ledger.connection.execute(
            "SELECT decision_id FROM sess_decisions"
            " WHERE activation_id = ? AND signal_date = ? AND event = ?",
            (preview.activation_id, preview.signal_date, event)).fetchone()
        if existing is not None:
            return
        row = self.ledger.connection.execute(
            "SELECT COALESCE(MAX(decision_id), 0) + 1 FROM sess_decisions"
        ).fetchone()
        self.ledger.connection.execute(
            "INSERT INTO sess_decisions (decision_id, activation_id,"
            " signal_date, snapshot_id, decision_hash, actions_json, plan_id,"
            " event, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (int(row[0]), preview.activation_id, preview.signal_date,
             preview.snapshot_id, preview.decision_hash,
             _canonical_json(preview.actions), plan_id, event, _now()))

    def decisions(self, activation_id: int | None = None) -> list[dict]:
        sql = ("SELECT decision_id, activation_id, signal_date, snapshot_id,"
               " decision_hash, actions_json, plan_id, event, created_at"
               " FROM sess_decisions")
        params: tuple = ()
        if activation_id is not None:
            sql += " WHERE activation_id = ?"
            params = (activation_id,)
        sql += " ORDER BY decision_id"
        return [{"decision_id": r[0], "activation_id": r[1],
                 "signal_date": r[2], "snapshot_id": r[3],
                 "decision_hash": r[4], "actions": json.loads(r[5]),
                 "plan_id": r[6], "event": r[7], "created_at": r[8]}
                for r in self.ledger.connection.execute(sql, params).fetchall()]

    # ── 账户桥与回放（§4：实际账户桥 + 因果查询 + 确定性重放） ──

    def _account_view(self, snapshot_id: int, signal_date: str,
                      ref_prices: dict):
        """计划层账户视图：当前投影 + 生效政策 + 参考价。"""
        projection = self.ledger.projection()
        policies = self.ledger.effective_policies(on_date=signal_date)
        holdings = {}
        total_value = projection.cash
        for symbol, projected in projection.holdings.items():
            avg_cost = (projected.cost_amount / projected.shares
                        if projected.cost_known and projected.shares > 0
                        else None)
            price = ref_prices.get(symbol)
            value = projected.shares * price if price else None
            if value is None:
                value = projected.shares * avg_cost if avg_cost else 0.0
            holdings[symbol] = HoldingView(
                symbol=symbol, shares=projected.shares, value=value,
                policy=policies.get(symbol, POLICY_OBSERVE),
                avg_cost=avg_cost)
            total_value += value
        return AccountView(snapshot_id=snapshot_id,
                           base_as_of=projection.base_as_of,
                           holdings=holdings, ref_prices=ref_prices,
                           total_value=total_value)

    def _entry_info(self, symbol: str, anchors: dict, seed_date: str,
                    base_as_of: str, effective_fills: list):
        """策略侧入场信息：启用后成交 > 启用锚点 > 政策锚点；都不存在返回 None。

        未知锚点绝不编造（A01）——调用方将其按 observe 处理并告警。
        """
        buys = [f for f in effective_fills
                if f.symbol == symbol and f.side == "BUY"
                and base_as_of < f.date <= seed_date]
        if buys:
            first = min(buys, key=lambda f: (f.date, f.fill_id))
            total_qty = sum(f.quantity for f in buys)
            avg = sum(f.quantity * f.price for f in buys) / total_qty
            return first.date, avg
        anchor = anchors.get(symbol)
        if anchor:
            return anchor["entry_date"], anchor["entry_price"]
        policy = self.ledger.holding_policy(symbol)
        if policy is not None and policy.anchor_date and policy.anchor_price:
            return policy.anchor_date, policy.anchor_price
        return None

    def _effective_fills(self):
        return [f for f in self.ledger.fills() if f.superseded_by is None]

    def _order_reasons(self) -> dict:
        try:
            return {o.client_order_id: o.reason
                    for o in self.plans.orders()}
        except Exception:  # noqa: BLE001 - 计划表未建时回放仍可用
            return {}

    def _replay_to(self, strategy, activation: ActivationRecord,
                   signal_date: str) -> _ReplayResult:
        """从启用边界确定性重放到信号日：实际事实驱动，策略钩子逐日演化。

        - 账户事实（成交/现金流/股数调整）全部来自账本；市场除权数据只
          用于价格锚点重缩放，不重复入账（A07）。
        - observe / 无锚点持仓进入 observe 簿：计资产与资金占用，但策略
          不可见（A01/A12）。
        - 不 pickle 策略；每次预览从 activation 边界全新重放，因此重启
          后与连续运行一致（A15），未来数据扰动不影响过去决策（A16）。
        """
        calendar = self.provider.get_calendar(activation.effective_date,
                                              signal_date)
        if not calendar:
            raise SessionValidationError(
                f"启用日 {activation.effective_date} 到信号日 {signal_date} "
                "无交易日——检查日历/行情覆盖")
        seed_date = self.provider.prev_trading_day(calendar[0])
        confirmed = self.ledger.confirmed_snapshot()
        if confirmed is None:
            raise SessionStateError("无确认快照——账户基准缺失")
        # seed 一致性：seed_date 缺失（启用日即首个已知交易日）时锚定
        # base 本身，避免 projection(None) 携带全部事实与逐日回放双计
        seed_as_of = seed_date if seed_date else confirmed.as_of
        projection = self.ledger.projection(as_of=seed_as_of)
        policies = self.ledger.effective_policies(on_date=signal_date)
        warnings: list[str] = []
        observe: dict[str, int] = {}
        effective_fills = self._effective_fills()
        account = self._seed_account(activation, projection, policies,
                                     seed_date, calendar[0], observe, warnings,
                                     effective_fills)
        equity = projection.cash + sum(
            h.shares * (h.entry_price or 0.0)
            for h in account.holdings.values())
        if equity <= 0:
            raise SessionStateError(
                f"回放种子权益非正（cash={projection.cash:.2f}）——"
                "账户事实不完整，无法初始化策略会话")
        engine = Engine(strategy, self.provider, initial_capital=equity)
        engine.account = account
        engine.prepare(calendar[0], signal_date)

        facts = self._facts_after(max(seed_as_of, projection.base_as_of))
        reason_map = self._order_reasons()
        observe_prices: dict[str, float] = {}

        def day_bars_of(day: str) -> dict:
            frame = engine.bars_by_date.get(day)
            return bars_to_dict(frame, day) if frame is not None else {}

        if seed_date:
            engine.compute_pending(seed_date, day_bars_of(seed_date), [])

        for today in calendar:
            bars_dict = day_bars_of(today)
            day_trades: list[Trade] = []
            day_facts = facts.get(today, {})
            for flow in day_facts.get("flows", ()):
                engine.account.cash += flow.amount
            for adjustment in day_facts.get("adjustments", ()):
                self._apply_share_adjustment(
                    engine, strategy, observe, adjustment, warnings)
            self._rescale_price_anchors(engine, strategy, today, bars_dict)
            for fill in day_facts.get("fills", ()):
                day_trades.append(self._apply_fill(
                    engine.account, observe, policies, fill, reason_map))
            self._update_observe_prices(observe, observe_prices, today)
            value_account(engine.account, bars_dict)
            extra = sum(observe[symbol] * observe_prices.get(symbol, 0.0)
                        for symbol in observe)
            engine.account.total_value += extra
            engine.compute_pending(today, bars_dict, day_trades)

        ref_prices = dict(observe_prices)
        for symbol, holding in engine.account.holdings.items():
            if holding.last_price > 0:
                ref_prices[symbol] = holding.last_price
        # 买入候选可能未持仓：补充信号日全截面收盘作参考价
        for symbol, bar in day_bars_of(signal_date).items():
            close = bar.get("close")
            if close and float(close) > 0:
                ref_prices[symbol] = float(close)
        conditions = dict(engine.account.holdings)
        return _ReplayResult(
            actions=dict(engine.pending_actions), conditions=conditions,
            ref_prices=ref_prices,
            total_value=engine.account.total_value, days=len(calendar),
            warnings=warnings)

    def _seed_account(self, activation: ActivationRecord, projection,
                      policies: dict, seed_date: str | None, replay_start: str,
                      observe: dict, warnings: list,
                      effective_fills: list) -> Account:
        """投影 + 锚点 → 引擎账户种子（回放起点）。"""
        account = Account(cash=projection.cash, initial_capital=1.0)
        account.total_value = projection.cash
        for symbol, projected in sorted(projection.holdings.items()):
            policy = policies.get(symbol, POLICY_OBSERVE)
            entry = self._entry_info(symbol, activation.anchors,
                                     seed_date or projection.base_as_of,
                                     projection.base_as_of, effective_fills)
            if policy == POLICY_OBSERVE:
                observe[symbol] = projected.shares
                continue
            if entry is None:
                warnings.append(
                    f"{symbol}（{policy}）缺入场锚点且无启用后买入记录——"
                    "按 observe 处理（策略不可见，仍计资产与资金占用）；"
                    "请补锚点（set_holding_policy/新 activation anchors）")
                observe[symbol] = projected.shares
                continue
            entry_date, entry_price = entry
            seed_days = self._holding_days_seed(entry_date, seed_date)
            account.holdings[symbol] = Holding(
                symbol=symbol, shares=projected.shares, entry_date=entry_date,
                entry_price=entry_price,
                cost=entry_price * projected.shares, last_price=entry_price,
                holding_days=seed_days,
                locked=bool(seed_date and entry_date >= replay_start))
        return account

    def _holding_days_seed(self, entry_date: str,
                           seed_date: str | None) -> int:
        """[entry_date, seed_date) 的交易日数（compute_pending(seed) 再 +1）。"""
        if not seed_date or entry_date >= seed_date:
            return 0
        calendar = self.provider.get_calendar(entry_date, seed_date)
        return max(0, len(calendar) - 1)

    def _facts_after(self, seed_date: str | None) -> dict:
        """seed_date 之后逐日分组的账本事实（成交/现金流/股数调整）。"""
        facts: dict = {}

        def bucket(date: str) -> dict:
            return facts.setdefault(date, {"flows": [], "adjustments": [],
                                           "fills": []})

        cutoff = seed_date or ""
        for flow in self.ledger.cash_flows():
            if flow.superseded_by is None and flow.date > cutoff:
                bucket(flow.date)["flows"].append(flow)
        for adjustment in self.ledger.share_adjustments():
            if adjustment.superseded_by is None and adjustment.date > cutoff:
                bucket(adjustment.date)["adjustments"].append(adjustment)
        for fill in self._effective_fills():
            if fill.date > cutoff:
                bucket(fill.date)["fills"].append(fill)
        for day in facts:
            facts[day]["fills"].sort(key=lambda f: f.fill_id)
        return facts

    def _apply_fill(self, account: Account, observe: dict, policies: dict,
                    fill, reason_map: dict) -> Trade:
        """把账本成交应用到回放账户（引擎口径：成本不含费用，T 日买入锁仓）。"""
        symbol = fill.symbol
        quantity = int(fill.quantity)
        price = float(fill.price)
        fees = (fill.fee_commission + fill.fee_tax + fill.fee_transfer
                + fill.fee_other)
        turnover = quantity * price
        if fill.side == "BUY":
            net = -(turnover) - fees
            account.cash += net
            holding = account.holdings.get(symbol)
            if holding is not None:
                total = holding.shares + quantity
                holding.entry_price = (
                    holding.entry_price * holding.shares + price * quantity
                ) / total
                holding.cost = holding.entry_price * total
                holding.shares = total
                holding.last_price = price
                holding.locked = True
            elif fill.plan_id or policies.get(symbol, POLICY_OBSERVE) != POLICY_OBSERVE:
                # 计划内执行的买入（策略自己的交易）直接进入策略可见簿；
                # 无策略绑定的人工新买入默认 observe，由用户显式接管
                account.holdings[symbol] = Holding(
                    symbol=symbol, shares=quantity, entry_date=fill.date,
                    entry_price=price, cost=price * quantity,
                    last_price=price, locked=True)
            else:
                observe[symbol] = observe.get(symbol, 0) + quantity
        else:
            net = turnover - fees
            account.cash += net
            holding = account.holdings.get(symbol)
            if holding is not None:
                holding.shares -= quantity
                if holding.shares <= 0:
                    del account.holdings[symbol]
                else:
                    holding.cost = holding.entry_price * holding.shares
            else:
                observe[symbol] = observe.get(symbol, 0) - quantity
                if observe[symbol] <= 0:
                    observe.pop(symbol, None)
        trigger = reason_map.get(fill.order_ref or "", "MANUAL")
        return Trade(
            date=fill.date, symbol=symbol, side=fill.side, trigger=trigger,
            price=price, shares=quantity, turnover=turnover,
            commission=fill.fee_commission + fill.fee_other,
            stamp_tax=fill.fee_tax, transfer_fee=fill.fee_transfer,
            slippage_amount=0.0, net_amount=net, reason=trigger)

    def _apply_share_adjustment(self, engine, strategy, observe: dict,
                                adjustment, warnings: list) -> None:
        """账本股数调整（送转实际到账）：改投影股数并重缩放价格锚点。"""
        symbol = adjustment.symbol
        delta = int(adjustment.delta_shares)
        holding = engine.account.holdings.get(symbol)
        if holding is not None:
            old = holding.shares
            new = old + delta
            if new <= 0:
                del engine.account.holdings[symbol]
                return
            scale = old / new
            holding.shares = new
            holding.entry_price *= scale
            holding.cost *= scale
        elif symbol in observe:
            old = observe[symbol]
            new = old + delta
            scale = old / new if new > 0 else 1.0
            if new <= 0:
                observe.pop(symbol, None)
                return
            observe[symbol] = new
        else:
            warnings.append(
                f"股数调整 {symbol} {delta:+d}（{adjustment.date}）无对应"
                "持仓——已按账本事实保留，请核对快照对账")
            return
        apply_condition_rescale(strategy, [{"symbol": symbol, "scale": scale}])

    def _rescale_price_anchors(self, engine, strategy, today: str,
                               bars_dict: dict) -> None:
        """现金分红除权：仅重缩放策略价格锚点（账户现金走账本事实，A07）。"""
        dividends = self.provider.get_dividends_on_date(today)
        if not dividends:
            return
        for symbol in list(engine.account.holdings):
            div = dividends.get(symbol)
            if not div or not div.get("cash_div"):
                continue
            bar = bars_dict.get(symbol) or {}
            pre_close = bar.get("pre_close", 0.0)
            if not pre_close or pre_close <= 0:
                logger.warning(
                    "[%s] %s 现金分红缺 pre_close，锚点不重缩放"
                    "（trailing 触发价可能偏高）", today, symbol)
                continue
            stk = div.get("stk_div", 0) or 0
            basis = pre_close * (1.0 + stk)
            scale = basis / (basis + div["cash_div"])
            holding = engine.account.holdings[symbol]
            holding.entry_price *= scale
            holding.cost *= scale
            apply_condition_rescale(strategy, [{"symbol": symbol,
                                                "scale": scale}])

    def _update_observe_prices(self, observe: dict, observe_prices: dict,
                               today: str) -> None:
        symbols = sorted(observe)
        if not symbols:
            return
        frame = self.provider.backend.query_bars(symbols, today, today)
        if frame is None or frame.empty:
            return
        try:
            for (date, symbol), row in frame.iterrows():
                close = row.get("close")
                if close and float(close) > 0:
                    observe_prices[symbol] = float(close)
        except TypeError:
            # 后端返回已置索引的帧（trade_date, symbol 多级索引）
            for symbol, row in frame.iterrows():
                close = row.get("close")
                if close and float(close) > 0:
                    observe_prices[symbol] = float(close)
