"""实盘 V2 策略会话测试 — docs/live_redesign.md §4（A13-16 及会话级 A01/A12/A14）。

独立期望：
- 只读预览：重复预览 decision_hash 相同且不写任何表；接受才落计划。
- 重启等价（A15）：重开账本 + 全新策略实例，同信号日预览 hash 一致。
- 因果（A16）：扰动信号日之后的行情/分红不改变该日决策。
- 观察簿：无锚点持仓策略不可见但仍计总资产；策略买入的 observe 持仓被丢弃。
- 版本固定：启用后换配置/换策略预览被拒；试算不改正式会话。
"""

import pandas as pd
import pytest

from btcore.backend import DataBackend
from btcore.provider import DataProvider
from btcore.strategy import Strategy
from research.live_account import AccountLedger
from research.live_plan import KIND_BUY, PlanStateError
from research.live_strategy import (
    SessionStateError,
    SessionValidationError,
    StrategySession,
)

DAYS = ["20240603", "20240604", "20240605", "20240606", "20240607",
        "20240610", "20240611", "20240612", "20240613", "20240614"]

BASE_PRICES = {
    "000001.SZ": {d: round(10.0 + 0.1 * i, 2) for i, d in enumerate(DAYS)},
    "600000.SH": {d: 20.0 for d in DAYS},
    "300750.SZ": {d: 50.0 for d in DAYS},
}

APPROX = pytest.approx


class MiniBackend(DataBackend):
    """可控合成后端：常量价格面板 + 可注入缺口日与分红。"""

    def __init__(self, prices=None, gap_days=(), dividends=None):
        self.prices = {s: dict(p) for s, p in (prices or BASE_PRICES).items()}
        self.gap_days = set(gap_days)
        self.dividends = dict(dividends or {})

    def get_calendar(self, start, end):
        return [d for d in DAYS if start <= d <= end]

    def get_dividends_on_date(self, date_str):
        return self.dividends.get(date_str, {})

    def query_bars(self, symbols, start, end, columns=None):
        rows = []
        for day in DAYS:
            if not (start <= day <= end) or day in self.gap_days:
                continue
            for symbol in sorted(self.prices):
                if symbols is not None and symbol not in symbols:
                    continue
                close = self.prices[symbol][day]
                rows.append({
                    "trade_date": day, "symbol": symbol,
                    "open": close, "high": close, "low": close, "close": close,
                    "vol": 10000.0, "adj_factor": 1.0, "pre_close": close,
                    "up_limit": round(close * 1.1, 2),
                    "down_limit": round(close * 0.9, 2),
                })
        if not rows:
            frame = pd.DataFrame(columns=[
                "trade_date", "symbol", "open", "high", "low", "close", "vol",
                "adj_factor", "pre_close", "up_limit", "down_limit"])
            return frame.set_index(["trade_date", "symbol"])
        frame = pd.DataFrame(rows).set_index(["trade_date", "symbol"])
        return frame if columns is None else frame[columns]

    def close(self):
        pass


class MiniStrategy(Strategy):
    """每 interval 个决策日买入一只未持有的固定序列标的。"""

    def on_start(self, provider, first_date, end_date=None):
        super().on_start(provider, first_date, end_date)
        self.decision_days = 0
        self._interval = int(self.config.get("interval", 3))

    def select(self, bars, snapshot, provider):
        self.decision_days += 1
        if not bars or (self.decision_days - 1) % self._interval != 0:
            return {"buy": [], "sell": []}
        held = set(snapshot.holdings)
        buys = [s for s in ("300750.SZ", "600000.SH") if s not in held][:1]
        return {"buy": buys, "sell": []}


def _strategy(interval=3):
    return MiniStrategy({
        "interval": interval, "max_positions": 5, "benchmark": "",
        "initial_capital": 120000,
    })


@pytest.fixture()
def ledger(tmp_path):
    led = AccountLedger.create(str(tmp_path / "sess.duckdb"),
                               account_id="sess-acc", broker="test")
    led.import_snapshot(
        as_of="20240603", source="broker", import_id="s1", cash=100000,
        holdings=[
            {"symbol": "000001.SZ", "shares": 1000, "available": 1000},
            {"symbol": "600000.SH", "shares": 500, "available": 500},
        ])
    try:
        yield led
    finally:
        led.close()


def _session(ledger, backend=None, strategy=None):
    return StrategySession.open(
        ledger, DataProvider(backend or MiniBackend()),
        strategy=strategy if strategy is not None else _strategy())


def _activated(ledger, backend=None, strategy=None):
    session = _session(ledger, backend, strategy)
    session.activate(
        effective_date="20240603",
        anchors={"000001.SZ": {"entry_date": "20240530",
                               "entry_price": 9.5}})
    return session


# ── 启用与政策桥 ──


def test_activate_records_identity_and_manage_anchored(ledger):
    session = _activated(ledger)
    record = session.active_activation()
    assert record.effective_date == "20240603"
    assert record.identity.name == "MiniStrategy"
    assert ledger.holding_policy("000001.SZ").policy == "managed"
    # 未锚点持仓保持 observe，不编造入场信息（A01）
    assert ledger.effective_policies()["600000.SH"] == "observe"


def test_activate_rejects_backdated_effective_date(ledger):
    session = _activated(ledger)
    session.activate(effective_date="20240604", strategy=_strategy(),
                     anchors={"000001.SZ": {"entry_date": "20240530",
                                            "entry_price": 9.5}})
    # 晚于基准的启用已存在：更早的启用日被拒（不重写启用前历史）
    with pytest.raises(SessionValidationError):
        session.activate(effective_date="20240603", strategy=_strategy(),
                         anchors={"000001.SZ": {"entry_date": "20240530",
                                                "entry_price": 9.5}})


def test_new_activation_supersedes_and_freezes_old(ledger):
    session = _activated(ledger)
    old = session.active_activation()
    session.activate(effective_date="20240605", strategy=_strategy(interval=5),
                     anchors={"000001.SZ": {"entry_date": "20240530",
                                            "entry_price": 9.5}})
    new = session.active_activation()
    assert new.activation_id > old.activation_id
    histories = {a.activation_id: a for a in session.activations()}
    assert histories[old.activation_id].status == "superseded"
    assert histories[new.activation_id].status == "active"


# ── 预览：可见簿 / 只读 / 幂等 ──


def test_preview_visible_book_and_observe_counts_capital(ledger):
    session = _activated(ledger)
    view = session.compute_preview(signal_date="20240603")
    # 锚点 managed 可见；observe 持仓策略不可见但仍计总资产（A12）
    assert view.visible_holdings == (("000001.SZ", 1000),)
    assert view.total_value == APPROX(100000 + 1000 * 10.0 + 500 * 20.0)
    # 首个决策日买入 300750：基数 120000/5=24000 → 400 股@50
    buys = [o for o in view.orders.orders if o.kind == KIND_BUY]
    assert [(o.symbol, o.planned_qty) for o in buys] == [("300750.SZ", 400)]


def test_preview_is_read_only_and_idempotent(ledger):
    session = _activated(ledger)
    first = session.compute_preview(signal_date="20240603")
    second = session.compute_preview(signal_date="20240603")
    assert first.decision_hash == second.decision_hash
    assert session.plans.plans() == []
    assert session.decisions() == []


def test_repeat_preview_does_not_advance_strategy_counter(ledger):
    """预览在工作副本上计算：重复预览不消耗调仓触发（A14 只读语义）。"""
    session = _activated(ledger)
    hashes = {session.compute_preview(signal_date=d).decision_hash
              for d in ("20240603", "20240603", "20240603")}
    assert len(hashes) == 1
    # 决策日 1 无动作（interval=3 → 1,4,7,10 行动）
    passive = session.compute_preview(signal_date="20240604")
    assert passive.orders.orders == ()


# ── 接受 / 重启等价 / 版本固定 ──


def test_accept_creates_plan_and_is_idempotent(ledger):
    session = _activated(ledger)
    preview = session.compute_preview(signal_date="20240603")
    plan = session.accept(preview)
    again = session.accept(preview)
    assert plan.plan_id == again.plan_id
    assert len(session.plans.orders(plan_id=plan.plan_id)) == 1
    events = [d["event"] for d in session.decisions()]
    assert events == ["accepted"]


def test_accept_rejects_stale_snapshot(ledger):
    session = _activated(ledger)
    preview = session.compute_preview(signal_date="20240603")
    # 快照推进（clean 自动确认）后旧预览不可接受，必须重算（A14）
    ledger.import_snapshot(
        as_of="20240604", source="broker", import_id="s2", cash=100000,
        holdings=[
            {"symbol": "000001.SZ", "shares": 1000, "available": 1000},
            {"symbol": "600000.SH", "shares": 500, "available": 500},
        ])
    with pytest.raises(PlanStateError):
        session.accept(preview)


def test_restart_equivalence_same_decision_hash(ledger, tmp_path):
    path = ledger.path
    session = _activated(ledger)
    before = session.compute_preview(signal_date="20240606")
    plan = session.accept(session.compute_preview(signal_date="20240603"))
    ledger.close()

    reopened = AccountLedger.open(path)
    try:
        session2 = StrategySession.open(reopened, DataProvider(MiniBackend()),
                                        strategy=_strategy())
        after = session2.compute_preview(signal_date="20240606")
        assert after.decision_hash == before.decision_hash
        assert session2.plans.plan(plan.plan_id).status == "active"
    finally:
        reopened.close()
    ledger_store = AccountLedger.open(path)
    ledger_store.close()


def test_version_drift_rejected_on_preview(ledger):
    _activated(ledger)
    drifted = _session(ledger, strategy=_strategy(interval=5))
    with pytest.raises(SessionStateError):
        drifted.compute_preview(signal_date="20240603")


def test_trial_does_not_touch_formal_session(ledger):
    session = _activated(ledger)
    trial = session.compute_trial(_strategy(interval=1),
                                  signal_date="20240603")
    assert trial.is_trial is True
    with pytest.raises(SessionStateError):
        session.accept(trial)
    assert session.plans.plans() == []
    assert session.decisions() == []
    assert session.active_activation().identity.config["interval"] == 3


# ── 因果与事实回放 ──


def test_future_perturbation_keeps_past_decision(ledger):
    backend = MiniBackend()
    session = _activated(ledger, backend)
    before = session.compute_preview(signal_date="20240605")
    # 扰动信号日之后的行情并注入未来分红 → 该日决策不变（A16）
    for day in ("20240606", "20240607", "20240610"):
        backend.prices["000001.SZ"][day] = 99.99
        backend.prices["300750.SZ"][day] = 1.0
    backend.dividends["20240607"] = {
        "000001.SZ": {"stk_div": 0.5, "cash_div": 1.0}}
    after = session.compute_preview(signal_date="20240605")
    assert after.decision_hash == before.decision_hash


def test_gap_day_fill_still_applied_and_hooks_skipped(ledger):
    backend = MiniBackend(gap_days={"20240605"})
    session = _activated(ledger, backend)
    # 缺口日的外部卖出事实照常入账（A16：缺行情不丢事实）
    ledger.record_fill(date="20240605", symbol="000001.SZ", side="SELL",
                       quantity=100, price=10.2, source="manual",
                       trade_id="gap-sell")
    view = session.compute_preview(signal_date="20240606")
    assert dict(view.visible_holdings)["000001.SZ"] == 900


def test_plan_bought_symbol_stays_visible_after_snapshot_advance(ledger):
    session = _activated(ledger)
    preview = session.compute_preview(signal_date="20240603")
    plan = session.accept(preview)
    order = plan.orders[0]
    session.plans.record_submission(order.client_order_id)
    session.plans.record_fill(order.client_order_id, date="20240604",
                              quantity=400, price=50.0, source="sim",
                              trade_id="t1")
    # 确认快照推进后重开：计划买入的 300750 仍是策略可见的 managed 持仓
    ledger.import_snapshot(
        as_of="20240604", source="broker", import_id="s2", cash=80000,
        holdings=[
            {"symbol": "000001.SZ", "shares": 1000, "available": 1000},
            {"symbol": "600000.SH", "shares": 500, "available": 500},
            {"symbol": "300750.SZ", "shares": 400, "available": 400},
        ])
    assert ledger.holding_policy("300750.SZ").policy == "managed"
    view = session.compute_preview(signal_date="20240604")
    visible = dict(view.visible_holdings)
    assert visible["300750.SZ"] == 400
    assert "600000.SH" not in visible


def test_strategy_buy_of_observe_holding_dropped_with_warning(ledger):
    class ObserveBuyer(MiniStrategy):
        def select(self, bars, snapshot, provider):
            return {"buy": ["600000.SH"], "sell": []}

    session = StrategySession.open(
        ledger, DataProvider(MiniBackend()),
        strategy=ObserveBuyer({"max_positions": 5, "benchmark": ""}))
    session.activate(effective_date="20240603")
    # 600000.SH 是 observe 持仓：策略买入指令被丢弃并告警，仓位保持
    view = session.compute_preview(signal_date="20240603")
    assert [o for o in view.orders.orders if o.kind == KIND_BUY] == []
    assert any("observe" in w for w in view.warnings)
    assert ledger.projection().holdings["600000.SH"].shares == 500


def test_readonly_session_can_preview(ledger, tmp_path):
    session = _activated(ledger)
    ledger.close()
    read_only = AccountLedger.open(session.ledger.path, read_only=True)
    try:
        ro_session = StrategySession.open(
            read_only, DataProvider(MiniBackend()), strategy=_strategy())
        view = ro_session.compute_preview(signal_date="20240603")
        assert view.decision_hash
    finally:
        read_only.close()
    restore = AccountLedger.open(session.ledger.path)
    restore.close()
