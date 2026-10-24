"""实盘 V2 账户层测试 — docs/live_redesign.md §3 与验收 A01-07/A20 账户层范围。

独立期望全部手算，不回读实现输出凑数：
- A05 净盈利 190 = (100×12-5) - (100×10+5)；现金守恒独立重算。
- A04 入金/出金只进净外部现金流，投资 PnL 恒为 0。
- A07 分红计投资收益、送转只记一次股数调整、不伪造成交。
"""

import hashlib
import json
import os

import duckdb
import pytest

from research.live_account import (
    CASH_DEPOSIT,
    CASH_DIVIDEND,
    CASH_TAX,
    CASH_WITHDRAWAL,
    POLICY_EXIT_ONLY,
    POLICY_MANAGED,
    POLICY_OBSERVE,
    SCHEMA_VERSION,
    SHARE_ADJ_CORPORATE_ACTION,
    AccountConflictError,
    AccountLedger,
    AccountStateError,
    AccountValidationError,
    LedgerPathError,
    LedgerSchemaError,
)

APPROX = pytest.approx


# ── 工具 ──


def file_hash(path: str) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def fact_dump(ledger: AccountLedger) -> dict:
    """事实表逻辑快照（排除审计表：拒绝审计是有意写入的诊断轨迹）。"""
    names = [row[0] for row in ledger.connection.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_name LIKE 'acct_%' ORDER BY table_name"
    ).fetchall()]
    return {
        name: ledger.connection.execute(f'SELECT * FROM "{name}"').fetchall()
        for name in names if name != "acct_audit"
    }


@pytest.fixture()
def ledger(tmp_path):
    led = AccountLedger.create(
        str(tmp_path / "live.duckdb"), account_id="acc-1", broker="test-broker")
    yield led
    led.close()


@pytest.fixture()
def ledger_path(tmp_path):
    return str(tmp_path / "live.duckdb")


def open_base(ledger, as_of="20260105", cash=10000.0, holdings=None,
              import_id="open-1", source="broker"):
    """建账期初快照（首次导入自动确认）。"""
    return ledger.import_snapshot(
        as_of=as_of, source=source, import_id=import_id, cash=cash,
        holdings=[] if holdings is None else holdings)


def holding_row(symbol, shares, available=None, cost=None):
    return {"symbol": symbol, "shares": shares, "available": available,
            "cost": cost}


# ── A01 中途建账：保存/确认快照，锚点不编造 ──


def test_a01_midaccount_opening_with_holdings_confirms(ledger):
    result = open_base(ledger, holdings=[holding_row("000001", 300, 200, 9.5),
                                         holding_row("600519", 100)])
    assert result.confirmed
    assert result.action == "opening_confirmed"
    snapshot = result.snapshot
    assert snapshot.as_of == "20260105"
    assert snapshot.holdings["000001"].shares == 300
    assert snapshot.holdings["000001"].available == 200
    assert snapshot.holdings["000001"].cost == APPROX(9.5)
    # status 立即可见最新确认快照（A02 前半）
    status = ledger.status()
    assert status.confirmed_snapshot.snapshot_id == snapshot.snapshot_id
    assert status.pending == ()
    # 投影从期初快照出发；无成交史也可继续
    projected = ledger.projection()
    assert projected.cash == APPROX(10000.0)
    assert projected.holdings["000001"].shares == 300


def test_a01_unknown_anchor_not_fabricated(ledger):
    open_base(ledger, holdings=[holding_row("000001", 100)])
    # 快照持仓无入场日期字段——账户层结构上不存在可编造的锚点
    snapshot = ledger.confirmed_snapshot()
    assert set(snapshot.holdings["000001"].__dict__) == {
        "symbol", "shares", "available", "cost"}
    # 无政策记录默认 observe；managed 必须显式补锚点
    assert ledger.holding_policy("000001") is None
    assert ledger.effective_policy("000001") == POLICY_OBSERVE
    with pytest.raises(AccountValidationError, match="anchor"):
        ledger.set_holding_policy("000001", POLICY_MANAGED, reason="接管")
    assert ledger.effective_policy("000001") == POLICY_OBSERVE
    ledger.set_holding_policy("000001", POLICY_MANAGED, reason="已补锚点",
                              anchor_date="20250901")
    assert ledger.effective_policy("000001") == POLICY_MANAGED


# ── A02 status 即时可见 + 只读打开不改变正式库 ──


def test_a02_readonly_open_leaves_ledger_unchanged(ledger, ledger_path):
    open_base(ledger, holdings=[holding_row("000001", 100)])
    ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT, amount=1000,
                            source="bank", event_id="dep1")
    before_dump = fact_dump(ledger)
    ledger.close()
    before_hash = file_hash(ledger_path)

    ro = AccountLedger.open(ledger_path, read_only=True)
    assert ro.account_id == "acc-1"
    status = ro.status()
    assert status.confirmed_snapshot is not None
    assert status.projected.cash == APPROX(11000.0)
    # 只读查询可算 PnL，入金不算盈亏（A04 口径交叉验证）
    view = ro.investment_pnl(opening_prices={"000001": 10.0},
                             closing_prices={"000001": 10.0})
    assert view.complete and view.investment_pnl == APPROX(0.0)
    ro.close()

    assert file_hash(ledger_path) == before_hash
    led = AccountLedger.open(ledger_path)
    assert fact_dump(led) == before_dump
    led.close()


def test_a02_readonly_connection_rejects_writes(ledger, ledger_path):
    open_base(ledger)
    ledger.close()
    ro = AccountLedger.open(ledger_path, read_only=True)
    with pytest.raises(AccountStateError):
        ro.import_snapshot(as_of="20260106", source="broker",
                           import_id="s2", cash=10000, holdings=[])
    with pytest.raises(AccountStateError):
        ro.record_cash_flow(date="20260106", category=CASH_DEPOSIT, amount=1,
                            source="bank", event_id="x")
    ro.close()


# ── A03 幂等与身份冲突 ──


def test_a03_snapshot_reimport_same_content_idempotent(ledger):
    first = open_base(ledger, holdings=[holding_row("000001", 100)])
    before = fact_dump(ledger)
    second = ledger.import_snapshot(
        as_of="20260105", source="broker", import_id="open-1", cash=10000,
        holdings=[holding_row("000001", 100)])
    assert second.action == "idempotent_noop"
    assert second.snapshot.snapshot_id == first.snapshot.snapshot_id
    assert fact_dump(ledger) == before


def test_a03_snapshot_same_identity_different_content_rejected(ledger):
    open_base(ledger, holdings=[holding_row("000001", 100)])
    before = fact_dump(ledger)
    with pytest.raises(AccountConflictError, match="内容不同") as excinfo:
        ledger.import_snapshot(as_of="20260105", source="broker",
                               import_id="open-1", cash=10500,
                               holdings=[holding_row("000001", 100)])
    assert excinfo.value.code == "conflict"
    assert fact_dump(ledger) == before


def test_a03_fill_batch_reimport_no_double_count(ledger):
    open_base(ledger)
    rows = [{"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": 10.0, "fee_commission": 5}]
    first = ledger.import_fills("broker", "stmt-1", rows)
    assert (first.appended, first.skipped) == (1, 0)
    second = ledger.import_fills("broker", "stmt-1", rows)
    assert (second.appended, second.skipped) == (0, 1)
    assert second.action == "idempotent_noop"
    assert len(ledger.fills()) == 1
    assert ledger.projection().cash == APPROX(10000 - 1005)
    # 无显式 trade_id 行的重入以 batch+row 身份判定；改内容即冲突
    with pytest.raises(AccountConflictError):
        ledger.import_fills("broker", "stmt-1", [
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": 10.5, "fee_commission": 5}])


def test_a03_different_trade_ids_same_content_both_kept(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "T1"},
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "T2"},
    ])
    fills = ledger.fills()
    assert [f.trade_id for f in fills] == ["T1", "T2"]
    assert ledger.projection().holdings["000001"].shares == 200


def test_a03_same_trade_id_different_content_rejected(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "T1"}])
    before = fact_dump(ledger)
    with pytest.raises(AccountConflictError, match="T1"):
        ledger.import_fills("broker", "stmt-2", [
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": 11.0, "trade_id": "T1"}])
    assert fact_dump(ledger) == before
    # 同 ID 同内容跨批重入仍幂等
    result = ledger.import_fills("broker", "stmt-2", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "T1"}])
    assert (result.appended, result.skipped) == (0, 1)
    assert len(ledger.fills()) == 1


def test_a03_row_identity_reorder_conflicts(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0},
        {"date": "20260106", "symbol": "600519", "side": "BUY",
         "quantity": 10, "price": 500.0},
    ])
    with pytest.raises(AccountConflictError):
        ledger.import_fills("broker", "stmt-1", [  # 顺序对调 → row 身份错位
            {"date": "20260106", "symbol": "600519", "side": "BUY",
             "quantity": 10, "price": 500.0},
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": 10.0},
        ])


# ── A04 入金/出金不是投资盈亏 ──


def test_a04_deposit_and_withdrawal_are_not_pnl(ledger):
    open_base(ledger)  # 期初：现金 10000，空仓
    ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT, amount=1000,
                            source="bank", event_id="dep-1")
    projected = ledger.projection()
    assert projected.cash == APPROX(11000.0)
    view = ledger.investment_pnl()
    assert view.complete
    assert view.investment_pnl == APPROX(0.0)  # 11000 - 10000 - 1000
    assert view.net_external_flows == APPROX(1000.0)

    ledger.record_cash_flow(date="20260107", category=CASH_WITHDRAWAL,
                            amount=-500, source="bank", event_id="wd-1")
    view = ledger.investment_pnl()
    assert view.complete
    assert view.investment_pnl == APPROX(0.0)  # 10500 - 10000 - 500
    assert ledger.projection().cash == APPROX(10500.0)


def test_a04_deposit_flow_idempotent_by_identity(ledger):
    open_base(ledger)
    first = ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT,
                                    amount=1000, source="bank", event_id="dep-1")
    again = ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT,
                                    amount=1000, source="bank", event_id="dep-1")
    assert again.flow_id == first.flow_id
    assert ledger.projection().cash == APPROX(11000.0)  # 不双记
    with pytest.raises(AccountConflictError):
        ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT,
                                amount=2000, source="bank", event_id="dep-1")


def test_a04_flow_category_signs_enforced(ledger):
    open_base(ledger)
    with pytest.raises(AccountValidationError):
        ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT,
                                amount=-100, source="bank", event_id="x1")
    with pytest.raises(AccountValidationError):
        ledger.record_cash_flow(date="20260106", category=CASH_WITHDRAWAL,
                                amount=100, source="bank", event_id="x2")
    with pytest.raises(AccountValidationError, match="类别"):
        ledger.record_cash_flow(date="20260106", category="adjust",
                                amount=100, source="bank", event_id="x3")
    assert ledger.cash_flows() == []


# ── A05 190 元净盈利与现金守恒 ──


def test_a05_roundtrip_net_profit_190(ledger):
    open_base(ledger)  # 期初现金 10000
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "fee_commission": 5, "trade_id": "B1"},
        {"date": "20260107", "symbol": "000001", "side": "SELL",
         "quantity": 100, "price": 12.0, "fee_commission": 5, "trade_id": "S1"},
    ])
    projected = ledger.projection()
    # 独立现金守恒：期初 - (买入金额+费) + (卖出金额-费)
    assert projected.cash == APPROX(10000 - (100 * 10 + 5) + (100 * 12 - 5))
    assert projected.cash == APPROX(10190.0)
    assert projected.holdings == {}
    view = ledger.investment_pnl()
    assert view.complete
    # 独立期望：净盈利 = 卖出净得 - 买入净支 = 1195 - 1005 = 190
    assert view.investment_pnl == APPROX((100 * 12 - 5) - (100 * 10 + 5))
    assert view.investment_pnl == APPROX(190.0)
    assert view.net_external_flows == APPROX(0.0)
    assert view.opening_equity == APPROX(10000.0)
    assert view.closing_equity == APPROX(10190.0)


def test_a05_partial_position_valued_with_prices(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "fee_commission": 5, "trade_id": "B1"},
    ])
    view = ledger.investment_pnl(opening_prices={}, closing_prices={"000001": 10.5})
    assert view.complete
    assert view.closing_equity == APPROX(8995 + 100 * 10.5)
    assert view.investment_pnl == APPROX(100 * 0.5 - 5)  # 浮盈 50 - 费 5


# ── A06 未确认差异不偷换状态；接管有审计 ──


def _buy_then_candidate(ledger):
    """期初 10000 空仓；买入后导入现金多 50 的快照 → 候选。"""
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "fee_commission": 5, "trade_id": "B1"}])
    result = ledger.import_snapshot(
        as_of="20260107", source="broker", import_id="s2", cash=9045,
        holdings=[holding_row("000001", 100)])
    return result


def test_a06_unexplained_diff_stays_candidate(ledger):
    result = _buy_then_candidate(ledger)
    assert result.action == "stored_candidate"
    assert not result.confirmed
    # 投影现金 = 10000 - 1005 = 8995；券商 9045 → 差 +50 保留待处理
    assert result.diff.cash.delta == APPROX(50.0)
    assert result.diff.holding_diffs == {}
    # 未确认前不静默改变账户：基准仍期初，投影仍 8995
    assert ledger.confirmed_snapshot().as_of == "20260105"
    assert ledger.projection().cash == APPROX(8995.0)
    status = ledger.status()
    assert len(status.pending) == 1
    assert status.pending[0].diff.cash.delta == APPROX(50.0)
    # 差异未解决 → PnL incomplete，不伪造精确收益
    view = ledger.investment_pnl(closing_prices={"000001": 10.0})
    assert not view.complete
    assert "pending_candidate:" + str(status.pending[0].snapshot_id) in view.reasons


def test_a06_classified_flow_then_reconcile_confirms(ledger):
    result = _buy_then_candidate(ledger)
    # 不明差额不得自动当作入金：必须显式分类（此处判定为分红）
    ledger.record_cash_flow(date="20260106", category=CASH_DIVIDEND, amount=50,
                            source="broker", event_id="div-1")
    review = ledger.reconcile_candidate(result.snapshot.snapshot_id)
    assert review.confirmed
    assert review.action == "reconciled_confirmed"
    assert ledger.confirmed_snapshot().as_of == "20260107"
    assert ledger.projection().cash == APPROX(9045.0)
    # 独立期望：PnL = 分红 50 - 买入费 5 = 45（仓位仍持有，按 10 元估值无浮盈）
    view = ledger.investment_pnl(closing_prices={"000001": 10.0})
    assert view.complete
    assert view.investment_pnl == APPROX(50.0 - 5.0)
    assert ledger.status().pending == ()


def test_a06_takeover_leaves_audit_and_gap(ledger):
    result = _buy_then_candidate(ledger)
    snapshot_id = result.snapshot.snapshot_id
    confirmed = ledger.takeover_candidate(snapshot_id, reason="历史交割单缺失，以券商为准")
    assert confirmed.status == "confirmed"
    takeovers = ledger.takeovers()
    assert len(takeovers) == 1
    detail = json.loads(takeovers[0].diff_json)
    assert detail["cash"]["expected"] == APPROX(8995.0)
    assert detail["cash"]["actual"] == APPROX(9045.0)
    assert detail["reason"] == "历史交割单缺失，以券商为准"
    # 审计有接管事件；新旧基准之间的买入被新基准吸收（superseded），不重复入账
    actions = [e.action for e in ledger.audit_events()]
    assert "snapshot_takeover" in actions
    fills = ledger.fills()
    assert fills[0].superseded_by == takeovers[0].takeover_id
    projected = ledger.projection()
    assert projected.cash == APPROX(9045.0)
    assert projected.holdings["000001"].shares == 100
    # 接管缺口 → 收益状态 incomplete，不发布伪精确收益率
    status = ledger.status()
    assert status.history_incomplete
    view = ledger.investment_pnl(closing_prices={"000001": 10.0})
    assert not view.complete
    assert view.investment_pnl is None
    assert view.opening_equity == APPROX(10000.0)  # 组件仍可见
    assert f"takeover_gap:{snapshot_id}" in view.reasons


def test_a06_takeover_requires_reason_and_latest(ledger):
    result = _buy_then_candidate(ledger)
    with pytest.raises(AccountValidationError):
        ledger.takeover_candidate(result.snapshot.snapshot_id, reason="  ")
    # 再导入一个更新的候选 → 旧候选不可接管
    newer = ledger.import_snapshot(
        as_of="20260108", source="broker", import_id="s3", cash=9045,
        holdings=[holding_row("000001", 100)])
    assert newer.action == "stored_candidate"
    with pytest.raises(AccountStateError, match="更新候选"):
        ledger.takeover_candidate(result.snapshot.snapshot_id, reason="x")
    ledger.takeover_candidate(newer.snapshot.snapshot_id, reason="以最新对账单接管")
    statuses = {s.snapshot_id: s.status for s in ledger.snapshots()}
    assert statuses[result.snapshot.snapshot_id] == "expired"
    assert statuses[newer.snapshot.snapshot_id] == "confirmed"


def test_a06_reconcile_requires_candidate_status(ledger):
    result = open_base(ledger)
    with pytest.raises(AccountStateError):
        ledger.reconcile_candidate(result.snapshot.snapshot_id)


# ── A07 分红/税费/送转：到账只记一次，不重复入账 ──


def test_a07_dividend_and_tax_are_investment_income(ledger):
    open_base(ledger)
    ledger.record_cash_flow(date="20260106", category=CASH_DIVIDEND, amount=100,
                            symbol="000001", source="broker", event_id="div-1")
    ledger.record_cash_flow(date="20260106", category=CASH_TAX, amount=-5,
                            source="broker", event_id="tax-1")
    view = ledger.investment_pnl()
    assert view.complete
    # 分红计投资收益、税费扣盈亏：+100 - 5 = 95，且不属于外部出入金
    assert view.investment_pnl == APPROX(95.0)
    assert view.net_external_flows == APPROX(0.0)
    assert view.closing_equity == APPROX(10095.0)


def test_a07_share_adjustment_recorded_once_no_fill(ledger):
    open_base(ledger, cash=5000, holdings=[holding_row("000001", 100, 100, 10.0)])
    first = ledger.record_share_adjustment(
        date="20260110", symbol="000001", delta_shares=100,
        category=SHARE_ADJ_CORPORATE_ACTION, reason="10送10",
        source="broker", event_id="adj-1")
    again = ledger.record_share_adjustment(
        date="20260110", symbol="000001", delta_shares=100,
        category=SHARE_ADJ_CORPORATE_ACTION, reason="10送10",
        source="broker", event_id="adj-1")
    assert again.adj_id == first.adj_id  # 同身份幂等，不重复加股
    assert ledger.projection().holdings["000001"].shares == 200
    assert ledger.fills() == []  # 送转不伪造成交
    # 实际到账快照与投影一致 → 直接确认，市场预测无需再入账
    result = ledger.import_snapshot(
        as_of="20260112", source="broker", import_id="s2", cash=5000,
        holdings=[holding_row("000001", 200)])
    assert result.confirmed
    # 独立期望：期初 100×10 + 5000 = 6000；期末 200×5.2 + 5000 = 6040
    view = ledger.investment_pnl(opening_prices={"000001": 10.0},
                                 closing_prices={"000001": 5.2})
    assert view.complete
    assert view.investment_pnl == APPROX(6040.0 - 6000.0)


# ── A20 非法输入明确拒绝且不污染 ──


BAD_SNAPSHOTS = [
    {"cash": float("nan"), "holdings": []},
    {"cash": float("inf"), "holdings": []},
    {"cash": -100, "holdings": []},
    {"cash": 100, "cash_balance": 100, "holdings": []},
    {"cash_balance": 100, "holdings": []},
    {"cash_balance": 100, "cash_available": 200, "holdings": []},
    {"cash_balance": 100, "cash_available": -1, "holdings": []},
    {"holdings": None},
    {"holdings": "empty"},
    {"holdings": [{"symbol": "000001", "shares": 100.5}]},
    {"holdings": [{"symbol": "000001", "shares": True}]},
    {"holdings": [{"symbol": "000001", "shares": 0}]},
    {"holdings": [{"symbol": "000001", "shares": -5}]},
    {"holdings": [{"symbol": "000001", "shares": 100, "available": 200}]},
    {"holdings": [{"symbol": "000001", "shares": 100},
                  {"symbol": "000001", "shares": 200}]},
    {"holdings": [{"symbol": "", "shares": 100}]},
    {"holdings": [{"symbol": "000001", "shares": 100, "cost": -1}]},
    {"cash": 10000, "holdings": [], "as_of": "2026-01-05"},
    {"cash": 10000, "holdings": [], "as_of": "20260230"},
    {"cash": 10000, "holdings": [], "as_of": "2026010"},
    {"cash": 10000, "holdings": [], "source": " "},
]


@pytest.mark.parametrize("overrides", BAD_SNAPSHOTS)
def test_a20_invalid_snapshot_inputs_rejected(ledger, overrides):
    before = fact_dump(ledger)
    kwargs = {"as_of": "20260105", "source": "broker", "import_id": "s1",
              "cash": 10000, "holdings": []}
    kwargs.update(overrides)
    with pytest.raises(AccountValidationError):
        ledger.import_snapshot(**kwargs)
    assert fact_dump(ledger) == before
    assert ledger.confirmed_snapshot() is None


BAD_FILL_ROWS = [
    {"date": "20260106", "symbol": "000001", "side": "BUY", "quantity": 100,
     "price": float("nan")},
    {"date": "20260106", "symbol": "000001", "side": "BUY", "quantity": 100,
     "price": -10},
    {"date": "20260106", "symbol": "000001", "side": "BUY", "quantity": 0,
     "price": 10},
    {"date": "20260106", "symbol": "000001", "side": "BUY", "quantity": 10.5,
     "price": 10},
    {"date": "20260106", "symbol": "000001", "side": "HOLD", "quantity": 100,
     "price": 10},
    {"date": "20260106", "symbol": 123, "side": "BUY", "quantity": 100,
     "price": 10},
    {"date": "2026/01/06", "symbol": "000001", "side": "BUY", "quantity": 100,
     "price": 10},
    {"date": "20260106", "symbol": "000001", "side": "BUY", "quantity": 100,
     "price": 10, "fee_commission": -5},
]


@pytest.mark.parametrize("row", BAD_FILL_ROWS)
def test_a20_invalid_fill_rows_rejected(ledger, row):
    open_base(ledger)
    before = fact_dump(ledger)
    with pytest.raises(AccountValidationError):
        ledger.import_fills("broker", "bad-1", [row])
    assert fact_dump(ledger) == before
    assert ledger.fills() == []


def test_a20_missing_path_rejected_without_creating(tmp_path):
    missing = str(tmp_path / "nope.duckdb")
    with pytest.raises(LedgerPathError):
        AccountLedger.open(missing)
    with pytest.raises(LedgerPathError):
        AccountLedger.open(missing, read_only=True)
    assert not os.path.exists(missing)


def test_a20_v1_ledger_rejected_without_modification(tmp_path):
    path = str(tmp_path / "v1.duckdb")
    conn = duckdb.connect(path)
    conn.execute("CREATE TABLE ledger_meta (key VARCHAR, value VARCHAR)")
    conn.execute("INSERT INTO ledger_meta VALUES ('schema_version', '1')")
    conn.execute("CREATE TABLE ledger_fills (id BIGINT, date VARCHAR)")
    conn.close()
    before = file_hash(path)
    with pytest.raises(LedgerSchemaError, match="V1"):
        AccountLedger.open(path)
    assert file_hash(path) == before


def test_a20_unknown_schema_version_rejected(ledger, ledger_path):
    open_base(ledger)
    ledger.connection.execute(
        "UPDATE acct_meta SET value = '99' WHERE key = 'schema_version'")
    ledger.close()
    with pytest.raises(LedgerSchemaError, match="99"):
        AccountLedger.open(ledger_path)


def test_a20_foreign_duckdb_rejected(tmp_path):
    path = str(tmp_path / "foreign.duckdb")
    conn = duckdb.connect(path)
    conn.execute("CREATE TABLE runs (run_id INTEGER)")
    conn.close()
    with pytest.raises(LedgerSchemaError, match="不是实盘 V2 账本库"):
        AccountLedger.open(path)


def test_a20_non_duckdb_file_rejected(tmp_path):
    path = str(tmp_path / "old.sqlite")
    with open(path, "wb") as handle:
        handle.write(b"SQLite format 3\x00" + b"\x00" * 128)
    with pytest.raises(LedgerSchemaError):
        AccountLedger.open(path)


def test_create_rejects_existing_file(ledger_path):
    led = AccountLedger.create(ledger_path, account_id="acc-1")
    led.close()
    with pytest.raises(AccountConflictError, match="已存在"):
        AccountLedger.create(ledger_path, account_id="acc-2")


def test_a20_rejection_leaves_ledger_usable(ledger):
    open_base(ledger)
    with pytest.raises(AccountValidationError):
        ledger.import_fills("broker", "bad-1", [
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": float("nan")}])
    # 拒绝后账本仍可正常入账（无残留锁/半事务）
    ledger.import_fills("broker", "ok-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "OK1"}])
    assert ledger.projection().cash == APPROX(9000.0)


# ── 原子失败：整批回滚不留半账本 ──


def test_atomic_batch_rollback_on_invalid_row(ledger):
    open_base(ledger)
    with pytest.raises(AccountValidationError):
        ledger.import_fills("broker", "stmt-bad", [
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": 10.0, "trade_id": "T1"},
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": float("inf"), "trade_id": "T2"},
        ])
    assert ledger.fills() == []
    counts = ledger.status().counts
    assert counts["fills"] == 0
    # 批次表不留半批记录
    rows = ledger.connection.execute(
        "SELECT COUNT(*) FROM acct_import_batches").fetchone()[0]
    assert rows == 0


def test_atomic_batch_rollback_on_oversell(ledger):
    open_base(ledger, holdings=[holding_row("000001", 100)])
    with pytest.raises(AccountStateError, match="超过投影持仓"):
        ledger.import_fills("broker", "stmt-over", [
            {"date": "20260106", "symbol": "000001", "side": "SELL",
             "quantity": 100, "price": 10.0, "trade_id": "S1"},
            {"date": "20260106", "symbol": "000001", "side": "SELL",
             "quantity": 50, "price": 10.0, "trade_id": "S2"},
        ])
    assert ledger.fills() == []
    assert ledger.projection().holdings["000001"].shares == 100


def test_oversell_single_fill_rejected_with_diagnosis(ledger):
    open_base(ledger, holdings=[holding_row("000001", 100)])
    with pytest.raises(AccountStateError, match="000001"):
        ledger.record_fill(date="20260106", symbol="000001", side="SELL",
                           quantity=200, price=10.0, source="broker",
                           trade_id="S9")
    assert ledger.fills() == []
    # 可诊断输入保留在拒绝审计中：错误消息含超卖股数
    rejections = [e for e in ledger.audit_events()
                  if e.action == "input_rejected" and e.subject_id == "fill_recorded"]
    assert rejections
    assert any("200" in json.loads(e.detail_json)["message"]
               for e in rejections)


# ── 修正与作废（§3.2 更正不改历史）──


def test_correction_changes_projection_keeps_history(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "fee_commission": 5, "trade_id": "B1"}])
    ledger.correct_fill(target_source="broker", target_trade_id="B1",
                        event_source="broker", event_id="fix-1",
                        date="20260108", fields={"price": 11.0})
    # 投影按修正后价格：10000 - (1100 + 5) = 8895
    projected = ledger.projection()
    assert projected.cash == APPROX(8895.0)
    assert projected.holdings["000001"].cost_amount == APPROX(1105.0)
    # 历史行未被改写：原始成交仍保留 price=10
    original = ledger.fills()[0]
    assert original.price == APPROX(10.0)
    corrections = ledger.fill_corrections()
    assert corrections[0].fields == {"price": 11.0}
    assert corrections[0].target_trade_id == "B1"


def test_void_fill_restores_projection_keeps_history(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "B1"}])
    ledger.void_fill(target_source="broker", target_trade_id="B1",
                     event_source="broker", event_id="void-1",
                     date="20260108", reason="券商回报错误重发")
    projected = ledger.projection()
    assert projected.cash == APPROX(10000.0)
    assert projected.holdings == {}
    assert len(ledger.fills()) == 1  # 历史行保留
    assert ledger.fill_corrections()[0].kind == "void"


def test_correction_idempotent_and_conflict(ledger):
    open_base(ledger)
    ledger.import_fills("broker", "stmt-1", [
        {"date": "20260106", "symbol": "000001", "side": "BUY",
         "quantity": 100, "price": 10.0, "trade_id": "B1"}])
    first = ledger.correct_fill(target_source="broker", target_trade_id="B1",
                                event_source="broker", event_id="fix-1",
                                date="20260108", fields={"price": 11.0})
    again = ledger.correct_fill(target_source="broker", target_trade_id="B1",
                                event_source="broker", event_id="fix-1",
                                date="20260108", fields={"price": 11.0})
    assert again.correction_id == first.correction_id
    with pytest.raises(AccountConflictError):
        ledger.correct_fill(target_source="broker", target_trade_id="B1",
                            event_source="broker", event_id="fix-1",
                            date="20260108", fields={"price": 12.0})


def test_correction_rejects_bad_fields_and_target(ledger):
    open_base(ledger)
    with pytest.raises(AccountValidationError, match="不支持"):
        ledger.correct_fill(target_source="broker", target_trade_id="B1",
                            event_source="broker", event_id="fix-1",
                            date="20260108", fields={"symbol": "600519"})
    with pytest.raises(AccountStateError):
        ledger.correct_fill(target_source="broker", target_trade_id="ghost",
                            event_source="broker", event_id="fix-2",
                            date="20260108", fields={"price": 11.0})


# ── 持仓管理政策（§3.4）──


def test_policy_expiry_restores_managed_only_with_anchor(ledger):
    open_base(ledger, holdings=[holding_row("000001", 100),
                                holding_row("600519", 10)])
    ledger.set_holding_policy("000001", POLICY_EXIT_ONLY, reason="手动保留",
                              expire_date="20260201", anchor_date="20250901")
    ledger.set_holding_policy("600519", POLICY_OBSERVE, reason="老股不知成本",
                              expire_date="20260201")
    assert ledger.effective_policy("000001", "20260131") == POLICY_EXIT_ONLY
    assert ledger.effective_policy("600519", "20260131") == POLICY_OBSERVE
    # 到期恢复规则：有锚点 → managed；无锚点 → 保持 observe
    assert ledger.effective_policy("000001", "20260201") == POLICY_MANAGED
    assert ledger.effective_policy("600519", "20260201") == POLICY_OBSERVE
    with pytest.raises(AccountValidationError):
        ledger.set_holding_policy("000001", POLICY_MANAGED, reason="x",
                                  anchor_date="20250901", expire_date="20260301")
    policies = ledger.effective_policies("20260131")
    assert policies == {"000001": POLICY_EXIT_ONLY, "600519": POLICY_OBSERVE}


def test_observe_holdings_still_counted_in_equity(ledger):
    open_base(ledger, cash=5000, holdings=[holding_row("000001", 100)])
    assert ledger.effective_policy("000001") == POLICY_OBSERVE
    view = ledger.equity({"000001": 11.0})
    assert view.total == APPROX(5000 + 1100.0)  # observe 仍计资产


# ── 边界：基准前事实 / 日期倒退 / 无基准 ──


def test_pre_base_fact_stored_without_projection_effect(ledger):
    open_base(ledger)
    fill = ledger.record_fill(date="20260105", symbol="000001", side="BUY",
                              quantity=100, price=10.0, source="broker",
                              trade_id="OLD1")
    assert fill.fill_id > 0  # 允许记录（审计留档）
    projected = ledger.projection()
    assert projected.cash == APPROX(10000.0)  # 基准已含该事实，不重复入账
    assert projected.holdings == {}
    assert projected.pre_base_fact_count == 1


def test_snapshot_date_regression_rejected(ledger):
    open_base(ledger, as_of="20260105")
    with pytest.raises(AccountValidationError, match="早于"):
        ledger.import_snapshot(as_of="20260104", source="broker",
                               import_id="s0", cash=10000, holdings=[])


def test_facts_require_confirmed_base(ledger):
    with pytest.raises(AccountStateError):
        ledger.record_fill(date="20260106", symbol="000001", side="BUY",
                           quantity=100, price=10.0, source="broker",
                           trade_id="T1")
    with pytest.raises(AccountStateError):
        ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT,
                                amount=100, source="bank", event_id="d1")
    with pytest.raises(AccountStateError):
        ledger.import_fills("broker", "b1", [
            {"date": "20260106", "symbol": "000001", "side": "BUY",
             "quantity": 100, "price": 10.0}])
    with pytest.raises(AccountStateError):
        ledger.projection()


def test_pnl_without_snapshot_reports_incomplete(ledger):
    view = ledger.investment_pnl()
    assert not view.complete
    assert view.reasons == ("no_confirmed_snapshot",)
    assert view.investment_pnl is None


def test_pnl_incomplete_when_prices_missing(ledger):
    open_base(ledger, cash=0, holdings=[holding_row("000001", 100)])
    view = ledger.investment_pnl()
    assert not view.complete
    assert view.investment_pnl is None
    assert "missing_opening_price:000001" in view.reasons
    assert "missing_closing_price:000001" in view.reasons
    equity = ledger.equity()
    assert equity.total is None
    assert equity.missing_prices == ("000001",)


# ── 简化资金输入与完整输入 ──


def test_cash_only_input_documents_frozen_zero(ledger):
    result = open_base(ledger, cash=8000)
    assert result.snapshot.cash_balance == APPROX(8000.0)
    assert result.snapshot.cash_available == APPROX(8000.0)  # 冻结按 0 解释
    full = ledger.import_snapshot(
        as_of="20260106", source="broker", import_id="s2",
        cash_balance=8000, cash_available=6000, holdings=[])
    # 无事实变动 → 与投影一致自动确认，available 如实保存
    assert full.confirmed
    assert full.snapshot.cash_available == APPROX(6000.0)


def test_ledger_identity_metadata(ledger):
    assert ledger.account_id == "acc-1"
    assert ledger.broker == "test-broker"
    status = ledger.status()
    assert status.schema_version == SCHEMA_VERSION
    assert status.counts["snapshots"] == 0


# ── 可组合事务（S2 组合语义：外层是唯一 commit/rollback owner）──


def test_composable_transaction_exact_repro():
    # 主控复现：外层 transaction 内 record_fill 曾抛
    # TransactionException: cannot start a transaction within a transaction
    led = AccountLedger.create(":memory:", account_id="probe")
    try:
        led.import_snapshot(as_of="20240603", source="manual", import_id="s1",
                            cash=10000, holdings=[])
        with led.transaction():
            led.record_fill(date="20240604", symbol="000001.SZ", side="BUY",
                            quantity=100, price=10, source="manual", trade_id="t1")
        assert led.projection().cash == APPROX(9000.0)
        assert len(led.fills()) == 1
    finally:
        led.close()


def test_composable_transaction_sentinel_and_fill_commit_together(tmp_path):
    # S2 场景：计划哨兵行 + 成交在同一组合事务提交，重开后仍都在
    path = str(tmp_path / "compose.duckdb")
    led = AccountLedger.create(path, account_id="acc-c")
    led.import_snapshot(as_of="20240603", source="manual", import_id="s1",
                        cash=10000, holdings=[])
    with led.transaction():
        led.connection.execute(
            "CREATE TABLE plan_sentinel (plan_id VARCHAR, status VARCHAR)")
        led.connection.execute(
            "INSERT INTO plan_sentinel VALUES ('P1', 'accepted')")
        led.record_fill(date="20240604", symbol="000001", side="BUY",
                        quantity=100, price=10, source="manual", trade_id="t1")
    led.close()
    ro = AccountLedger.open(path, read_only=True)
    assert ro.connection.execute(
        "SELECT * FROM plan_sentinel").fetchall() == [("P1", "accepted")]
    assert len(ro.fills()) == 1
    assert ro.projection().cash == APPROX(9000.0)
    ro.close()


def test_composable_transaction_failure_rolls_back_all(tmp_path):
    # 异常穿出 with：外层统一回滚——哨兵表（含 DDL）、成交都不留
    path = str(tmp_path / "compose.duckdb")
    led = AccountLedger.create(path, account_id="acc-c")
    led.import_snapshot(as_of="20240603", source="manual", import_id="s1",
                        cash=10000, holdings=[])
    with pytest.raises(AccountStateError, match="超过投影持仓"):
        with led.transaction():
            led.connection.execute("CREATE TABLE plan_sentinel (plan_id VARCHAR)")
            led.connection.execute("INSERT INTO plan_sentinel VALUES ('P1')")
            led.record_fill(date="20240604", symbol="000001", side="BUY",
                            quantity=100, price=10, source="manual", trade_id="t1")
            led.record_fill(date="20260605", symbol="000001", side="SELL",
                            quantity=200, price=10, source="manual", trade_id="t2")
    assert led.fills() == []
    assert led.projection().cash == APPROX(10000.0)
    tables = {row[0] for row in led.connection.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    assert "plan_sentinel" not in tables
    led.close()
    ro = AccountLedger.open(path, read_only=True)
    assert ro.fills() == []
    assert ro.projection().cash == APPROX(10000.0)
    ro.close()


def test_composable_transaction_nested_exception_caught_no_partial(tmp_path):
    # 嵌套异常被捕获后仍不得提交部分更新：外层 rollback-only，
    # 明确抛 AccountStateError 且整体回滚（含捕获后继续写入的内容）
    path = str(tmp_path / "compose.duckdb")
    led = AccountLedger.create(path, account_id="acc-c")
    led.import_snapshot(as_of="20240603", source="manual", import_id="s1",
                        cash=10000, holdings=[])
    with pytest.raises(AccountStateError, match="rollback-only"):
        with led.transaction():
            led.connection.execute("CREATE TABLE plan_sentinel (plan_id VARCHAR)")
            led.connection.execute("INSERT INTO plan_sentinel VALUES ('P1')")
            with pytest.raises(AccountStateError):
                led.import_fills("manual", "batch-1", [
                    {"date": "20240604", "symbol": "000001", "side": "BUY",
                     "quantity": 100, "price": 10.0, "trade_id": "ok1"},
                    {"date": "20240605", "symbol": "000001", "side": "SELL",
                     "quantity": 300, "price": 10.0, "trade_id": "bad1"},
                ])
            # 捕获后继续写：调用可成功，但最终随外层整体回滚
            led.record_cash_flow(date="20240606", category=CASH_DEPOSIT,
                                 amount=500, source="bank", event_id="d1")
    assert led.fills() == []  # 失败批次零残留
    assert led.cash_flows() == []  # 捕获后的写入也回滚
    tables = {row[0] for row in led.connection.execute(
        "SELECT table_name FROM information_schema.tables").fetchall()}
    assert "plan_sentinel" not in tables  # DDL 一并回滚
    assert led.projection().cash == APPROX(10000.0)
    # 深度/标志复位：新事务正常提交
    with led.transaction():
        led.record_cash_flow(date="20240607", category=CASH_DEPOSIT, amount=200,
                             source="bank", event_id="d2")
    assert led.projection().cash == APPROX(10200.0)
    led.close()
    ro = AccountLedger.open(path, read_only=True)
    assert ro.fills() == []
    assert len(ro.cash_flows()) == 1  # 仅复位后的新事务留存
    ro.close()


def test_composable_transaction_nested_public_transaction(ledger):
    open_base(ledger)
    with ledger.transaction():
        with ledger.transaction():
            ledger.record_cash_flow(date="20260106", category=CASH_DEPOSIT,
                                    amount=100, source="bank", event_id="d1")
    assert ledger.projection().cash == APPROX(10100.0)


def test_transaction_rejected_on_readonly(ledger, ledger_path):
    open_base(ledger)
    ledger.close()
    ro = AccountLedger.open(ledger_path, read_only=True)
    with pytest.raises(AccountStateError):
        with ro.transaction():
            pass
    ro.close()


def test_caught_nested_failure_aborts_outer_transaction():
    # 主控独立复现：外层 INSERT 1；嵌套 transaction INSERT 2 后 raise ValueError
    # 被捕获。独立期望：外层退出明确抛 AccountStateError 且表为空（整体回滚）。
    led = AccountLedger.create(":memory:", account_id="probe")
    try:
        led.import_snapshot(as_of="20240603", source="manual", import_id="s1",
                            cash=10000, holdings=[])
        led.connection.execute("CREATE TABLE sentinel(n INTEGER)")
        with pytest.raises(AccountStateError, match="嵌套"):
            with led.transaction():
                led.connection.execute("INSERT INTO sentinel VALUES (1)")
                try:
                    with led.transaction():
                        led.connection.execute("INSERT INTO sentinel VALUES (2)")
                        raise ValueError("boom")
                except ValueError:
                    pass
        assert led.connection.execute("SELECT * FROM sentinel").fetchall() == []

        # 公共写方法失败（写前校验/投影校验）被捕获：同样 rollback-only
        with pytest.raises(AccountStateError):
            with led.transaction():
                led.connection.execute("INSERT INTO sentinel VALUES (3)")
                try:
                    led.record_fill(date="20240604", symbol="000001", side="SELL",
                                    quantity=999, price=10, source="manual",
                                    trade_id="s9")  # 超卖
                except AccountStateError:
                    pass
        assert led.connection.execute("SELECT * FROM sentinel").fetchall() == []

        # 事务外纯校验失败被捕获：也要 rollback-only，不留半提交边界
        with pytest.raises(AccountStateError):
            with led.transaction():
                led.connection.execute("INSERT INTO sentinel VALUES (4)")
                try:
                    led.record_cash_flow(date="2026/01/01",
                                         category=CASH_DEPOSIT, amount=100,
                                         source="bank", event_id="bad-date")
                except AccountValidationError:
                    pass
        assert led.connection.execute("SELECT * FROM sentinel").fetchall() == []

        # 深度/标志复位：后续新事务正常提交
        with led.transaction():
            led.connection.execute("INSERT INTO sentinel VALUES (7)")
        assert led.connection.execute(
            "SELECT * FROM sentinel").fetchall() == [(7,)]
    finally:
        led.close()
