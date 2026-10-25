"""实盘 V2 计划层测试 — docs/live_redesign.md §5 与验收 A08-14/A17-19/A24 计划层范围。

独立期望全部手算，不回读实现输出凑数：
- A09 等权：10000 现金、90000 持仓、两只新买、max_positions=5 → 基数
  20000，现金顺序递减后 5000+5000（费用前），绝不是 15000。
- A11 卖后买：回款只是依赖条件，未成交不可执行；部分回款按成交比例
  释放相应预算。
- A19 原子性：成交回报失败（超计划/价格偏离/未提交）时订单与账本
  成交都不留半笔。
"""

import pytest

from research.live_account import (
    POLICY_EXIT_ONLY,
    POLICY_MANAGED,
    POLICY_OBSERVE,
    AccountLedger,
)
from research.live_plan import (
    KIND_BUY,
    KIND_ENTRY_COND,
    KIND_PROTECT,
    KIND_SELL,
    ORDER_CANCELLED,
    ORDER_EXPIRED,
    ORDER_FILLED,
    ORDER_PARTIAL,
    ORDER_REJECTED,
    ORDER_SUBMITTED,
    ORDER_UNKNOWN,
    PLAN_ACTIVE,
    PLAN_CANCELLED,
    PLAN_COMPLETED,
    PLAN_EXPIRED,
    PLAN_PARTIAL,
    PLAN_SKIPPED,
    PLAN_SUPERSEDED,
    AccountView,
    BudgetContext,
    ExecutionConfig,
    HoldingView,
    ManualExecutor,
    PlanConflictError,
    PlanStateError,
    PlanStore,
    PlanValidationError,
    SimulatedExecutor,
    UnsupportedConditionError,
    apply_execution,
    build_plan,
    gate_order,
)

APPROX = pytest.approx

ZERO_FEES = ExecutionConfig(max_positions=5, commission_rate=0.0,
                            min_commission=0.0, stamp_tax_rate=0.0,
                            transfer_fee_rate=0.0)


# ── 工具 ──


def hold(symbol, shares, price, policy=POLICY_MANAGED):
    return HoldingView(symbol=symbol, shares=shares, value=shares * price,
                       policy=policy, avg_cost=price)


def view(holdings, ref_prices, total_value, snapshot_id=1,
         base_as_of="20240105"):
    return AccountView(snapshot_id=snapshot_id, base_as_of=base_as_of,
                       holdings=dict(holdings), ref_prices=dict(ref_prices),
                       total_value=total_value)


def budget(cash_available=10000.0, reserved=0.0, blocked=None):
    return BudgetContext(cash_available=cash_available,
                         outstanding_reserved=reserved, blocked_reason=blocked)


def draft_of(actions, conditions=None, holdings=None, ref=None,
             cash_available=10000.0, reserved=0.0, blocked=None,
             total_value=None, config=None):
    holdings = holdings or {}
    ref = ref or {}
    total = total_value if total_value is not None else (
        cash_available + sum(h.value for h in holdings.values()))
    return build_plan(actions, conditions or {}, view(holdings, ref, total),
                      budget(cash_available, reserved, blocked),
                      config or ZERO_FEES)


def by_kind(draft, kind):
    return [o for o in draft.orders if o.kind == kind]


def by_symbol(draft, symbol):
    return [o for o in draft.orders if o.symbol == symbol]


@pytest.fixture()
def ledger(tmp_path):
    led = AccountLedger.create(str(tmp_path / "plan.duckdb"),
                               account_id="plan-acc", broker="test")
    led.import_snapshot(as_of="20240105", source="broker", import_id="s1",
                        cash=10000, holdings=[
                            {"symbol": "AAA", "shares": 1000,
                             "available": 1000}])
    try:
        yield led
    finally:
        led.close()


@pytest.fixture()
def store(ledger):
    return PlanStore.attach(ledger)


def make_draft_for_store(actions=None, cash=10000.0):
    """给 store 测试用：按买入名单提供参考价的最小草稿。"""
    actions = actions or {"buy": ["BBB"]}
    ref = {s: 10.0 for s in sorted(set(actions.get("buy") or []))}
    return draft_of(actions, holdings={}, ref=ref,
                    cash_available=cash, config=ZERO_FEES)


# ── A09 等权预算：现金顺序递减 ──


def test_a09_equal_weight_two_buys_share_cash_not_15000():
    holdings = {"CCC": hold("CCC", 9000, 10.0)}
    ref = {"CCC": 10.0, "AAA": 10.0, "BBB": 10.0}
    draft = draft_of({"buy": ["AAA", "BBB"]}, holdings=holdings, ref=ref,
                     cash_available=10000.0)
    buys = by_kind(draft, KIND_BUY)
    assert [o.symbol for o in buys] == ["AAA", "BBB"]
    # 费用前各 5000：min(基数 20000, 现金/n_left)
    assert [o.planned_qty for o in buys] == [500, 500]
    assert [o.est_value for o in buys] == [APPROX(5000.0), APPROX(5000.0)]
    assert sum(o.est_value for o in buys) <= 10000.0
    # 10000 现金绝不能预算出 15000
    assert sum(o.est_value for o in buys) < 15000.0


def test_default_fees_keep_budget_conservation():
    config = ExecutionConfig(max_positions=5)
    holdings = {"CCC": hold("CCC", 9000, 10.0)}
    ref = {"CCC": 10.0, "AAA": 10.0, "BBB": 10.0}
    draft = draft_of({"buy": ["AAA", "BBB"]}, holdings=holdings, ref=ref,
                     cash_available=10000.0, config=config)
    buys = by_kind(draft, KIND_BUY)
    assert buys, "默认费率下仍应有两笔买入"
    total_spend = sum(o.est_value + o.est_fees for o in buys)
    assert total_spend <= 10000.0 + 1e-9
    assert sum(o.budget_reserved for o in buys) <= 10000.0 + 1e-9


def test_cash_buffer_reduces_spendable():
    config = ExecutionConfig(max_positions=1, cash_buffer=2000.0,
                             commission_rate=0.0, min_commission=0.0,
                             stamp_tax_rate=0.0, transfer_fee_rate=0.0)
    draft = draft_of({"buy": ["AAA"]}, ref={"AAA": 10.0},
                     cash_available=10000.0, config=config)
    buys = by_kind(draft, KIND_BUY)
    assert len(buys) == 1
    assert buys[0].budget_reserved <= 8000.0 + 1e-9


# ── A08 target_value 语义 ──


def test_a08_target_value_diff_conversion_and_clear():
    holdings = {"AAA": hold("AAA", 500, 10.0), "BBB": hold("BBB", 450, 8.0)}
    ref = {"AAA": 10.0, "BBB": 8.0, "CCC": 10.0}
    draft = draft_of({"target_value": {"AAA": 3000.0, "BBB": 0.0,
                                       "CCC": 5000.0}},
                     holdings=holdings, ref=ref, cash_available=6000.0)
    sells = {o.symbol: o for o in by_kind(draft, KIND_SELL)}
    buys = {o.symbol: o for o in by_kind(draft, KIND_BUY)}
    # AAA 现值 5000 → 目标 3000：卖 200 股（整手）
    assert sells["AAA"].planned_qty == 200
    assert sells["AAA"].reason == "TARGET"
    # BBB 清仓：零股一并卖出
    assert sells["BBB"].planned_qty == 450
    assert sells["BBB"].detail["full_exit"] is True
    # CCC 新目标 5000 → 现金 6000 内买入 500 股
    assert buys["CCC"].planned_qty == 500
    # 未列出的持仓不动：draft 中没有任何针对其他 symbol 的意外订单
    assert {o.symbol for o in draft.orders} == {"AAA", "BBB", "CCC"}


def test_a08_partial_reduce_floors_to_lots():
    holdings = {"AAA": hold("AAA", 500, 10.0)}
    draft = draft_of({"target_value": {"AAA": 1000.0}}, holdings=holdings,
                     ref={"AAA": 10.0}, cash_available=0.0)
    sells = by_kind(draft, KIND_SELL)
    # 现值 5000 → 目标 1000：需减 4000 → 400 股
    assert len(sells) == 1 and sells[0].planned_qty == 400


def test_a13_position_tolerance_skips_small_adjustment():
    holdings = {"AAA": hold("AAA", 500, 10.0)}
    config = ExecutionConfig(max_positions=5, position_tolerance=300.0,
                             commission_rate=0.0, min_commission=0.0,
                             stamp_tax_rate=0.0, transfer_fee_rate=0.0)
    draft = draft_of({"target_value": {"AAA": 5200.0}}, holdings=holdings,
                     ref={"AAA": 10.0}, config=config)
    assert draft.orders == ()
    assert any("容差" in s["reason"] for s in draft.skipped)


def test_a13_reduced_buy_after_skip_regenerates_from_reality():
    """跳过/少买后从现实重新生成计划，不追旧目标。

    回测等权口径：基数 = 总资产/max_positions；10000/5=2000 → 200 股；
    现金剩 6000 时基数 1200 → 120 股，始终从当前现实出发。
    """
    first = draft_of({"buy": ["AAA"]}, ref={"AAA": 10.0},
                     cash_available=10000.0)
    assert by_kind(first, KIND_BUY)[0].planned_qty == 200
    # 现实变化：现金只剩 6000（此前部分执行），重新预览
    second = draft_of({"buy": ["AAA"]}, ref={"AAA": 10.0},
                      cash_available=6000.0)
    assert by_kind(second, KIND_BUY)[0].planned_qty == 100
    # 现金 3000 时基数 600 不足一手 → 明确跳过而不是退半单
    third = draft_of({"buy": ["AAA"]}, ref={"AAA": 10.0},
                     cash_available=3000.0)
    assert by_kind(third, KIND_BUY) == []
    assert any("不足" in s["reason"] for s in third.skipped)


# ── A10 权重/费用/整手/预留 ──


def test_a10_weights_fees_lots_reservation_conservation():
    config = ExecutionConfig(max_positions=5, commission_rate=0.001,
                             min_commission=5.0, stamp_tax_rate=0.0,
                             transfer_fee_rate=0.0)
    holdings = {"OLD": hold("OLD", 1000, 10.0)}
    ref = {"OLD": 10.0, "AAA": 10.0, "BBB": 30.0}
    draft = draft_of({"buy": ["AAA", "BBB"],
                      "buy_weights": {"AAA": 0.5, "BBB": 0.3}},
                     holdings=holdings, ref=ref, cash_available=8000.0,
                     config=config)
    buys = {o.symbol: o for o in by_kind(draft, KIND_BUY)}
    # total_value = 8000 + 10000 = 18000；AAA 0.5 → 9000 受现金约束截断，
    # BBB 0.3 → 5400 > 30×180=5400 也受约束；总支出+费用 ≤ 8000
    assert sum(o.est_value + o.est_fees
               for o in buys.values()) <= 8000.0 + 1e-9
    assert all(o.planned_qty % 100 == 0 for o in buys.values())


def test_a10_insufficient_for_one_lot_skips_with_reason():
    # 250 元买 30 元股票不足一手
    draft = draft_of({"buy": ["BBB"]}, ref={"BBB": 30.0},
                     cash_available=250.0)
    assert draft.orders == ()
    assert any("不足" in s["reason"] for s in draft.skipped)


def test_a10_outstanding_reservation_reduces_spendable(ledger, store):
    draft1 = make_draft_for_store({"buy": ["BBB"]}, cash=10000.0)
    store.accept_plan(draft1, activation_id=1, snapshot_id=1,
                      signal_date="20240105", trade_date="20240108",
                      expiry_date="20240108",
                      decision_hash=draft1.fingerprint())
    # 等权基数 10000/5 = 2000 → 预留 2000
    assert store.outstanding_reserved() == APPROX(2000.0)
    ctx = store.budget_context()
    assert ctx.spendable == APPROX(8000.0)
    # 新预览只能用剩余 8000：基数 2000 → 200 股
    draft2 = draft_of({"buy": ["CCC"]}, ref={"CCC": 10.0},
                      cash_available=10000.0, reserved=2000.0)
    buys = by_kind(draft2, KIND_BUY)
    assert buys[0].planned_qty == 200


# ── 阻止项：待确认差异 / 负可用现金；接管后不阻止 ──


def test_pending_candidate_blocks_buys_not_sells(ledger, store):
    # AAA 投影外多 200 股 → 候选差异
    ledger.import_snapshot(as_of="20240108", source="broker",
                           import_id="s2", cash=10000,
                           holdings=[{"symbol": "AAA", "shares": 1200,
                                      "available": 1200}])
    ctx = store.budget_context()
    assert ctx.blocked_reason is not None
    assert ctx.spendable is None
    draft = draft_of({"buy": ["BBB"], "sell": ["AAA"]},
                     holdings={"AAA": hold("AAA", 1200, 10.0)},
                     ref={"AAA": 10.0, "BBB": 10.0},
                     cash_available=10000.0,
                     blocked=ctx.blocked_reason)
    assert by_kind(draft, KIND_BUY) == []
    assert any("预算被阻止" in s["reason"] for s in draft.skipped)
    # 卖出不受阻
    assert by_kind(draft, KIND_SELL)


def test_negative_available_cash_blocks_budget(ledger, store):
    # 未记入金的买入使投影现金为负（事实不完整）
    ledger.record_fill(date="20240108", symbol="BBB", side="BUY",
                       quantity=1200, price=10.0, source="manual",
                       trade_id="t1")
    ctx = store.budget_context()
    assert ctx.blocked_reason is not None and "负" in ctx.blocked_reason


def test_confirmed_takeover_keeps_budget_open_but_pnl_incomplete(ledger, store):
    ledger.record_fill(date="20240108", symbol="BBB", side="BUY",
                       quantity=100, price=10.0, source="manual",
                       trade_id="t1")
    result = ledger.import_snapshot(
        as_of="20240109", source="broker", import_id="s3", cash=10000,
        holdings=[{"symbol": "AAA", "shares": 1000, "available": 1000},
                  {"symbol": "BBB", "shares": 100, "available": 100}])
    assert result.action == "stored_candidate"
    ledger.takeover_candidate(result.snapshot.snapshot_id, reason="转入持仓")
    ctx = store.budget_context()
    # 已显式接管确认：预算不被 takeover_gap 永久封锁
    assert ctx.blocked_reason is None
    assert ctx.spendable is not None
    pnl = ledger.investment_pnl(closing_prices={"AAA": 10.0, "BBB": 10.0})
    assert pnl.complete is False
    assert any(r.startswith("takeover_gap:") for r in pnl.reasons)


# ── A11 卖后买依赖与部分回款释放 ──


def test_a11_sell_before_buy_dependency_and_partial_release(ledger, store):
    holdings = {"AAA": hold("AAA", 1000, 10.0)}
    ref = {"AAA": 10.0, "BBB": 10.0}
    # 现金 1000，卖出 AAA 1000 股（回款 ~10000），等权买 BBB：
    # 基数 (1000+10000)/5=2200 → 资金分解：现金 1000 + 回款依赖 ~1200
    draft = build_plan({"sell": ["AAA"], "buy": ["BBB"]}, {},
                       view(holdings, ref, 11000.0),
                       budget(cash_available=1000.0), ZERO_FEES)
    sells = by_kind(draft, KIND_SELL)
    buys = by_kind(draft, KIND_BUY)
    assert len(sells) == 1 and len(buys) == 1
    buy = buys[0]
    assert buy.planned_qty == 200  # 2200 → 200 股 @10
    assert buy.budget_reserved == APPROX(1000.0)
    assert sum(buy.proceeds_alloc.values()) > 0
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    sell_order = [o for o in plan.orders if o.kind == KIND_SELL][0]
    buy_order = [o for o in plan.orders if o.kind == KIND_BUY][0]
    # 未成交：买单不可执行
    gate = gate_order(store, buy_order.client_order_id)
    assert gate.allowed is False
    assert "依赖" in gate.reason
    # 部分成交 400/1000：按比例释放回款 → 可执行 100 股（现金1000+402 → 140 股取整手 100）
    store.record_submission(sell_order.client_order_id)
    store.record_fill(sell_order.client_order_id, date="20240108",
                      quantity=400, price=10.0, source="sim",
                      trade_id="f1")
    gate = gate_order(store, buy_order.client_order_id)
    assert gate.allowed is True and gate.quantity == 100
    # 全部成交后解锁 200 股
    store.record_fill(sell_order.client_order_id, date="20240108",
                      quantity=600, price=10.0, source="sim",
                      trade_id="f2")
    gate = gate_order(store, buy_order.client_order_id)
    assert gate.allowed is True and gate.quantity == 200


def test_a11_full_sell_withdraws_same_symbol_protections():
    holdings = {"AAA": hold("AAA", 1000, 10.0)}
    conds = {"AAA": [{"type": "STOP_LOSS", "price": 9.0}]}
    draft = draft_of({"sell": ["AAA"]}, conditions=conds, holdings=holdings,
                     ref={"AAA": 10.0})
    assert by_kind(draft, KIND_PROTECT) == []
    assert any("撤销" in w or "缩减" in w for w in draft.warnings)


def test_a11_partial_sell_shrinks_protection_quantity():
    holdings = {"AAA": hold("AAA", 1000, 10.0)}
    conds = {"AAA": [{"type": "STOP_LOSS", "price": 9.0}]}
    draft = draft_of({"sell": ["AAA"], "sell_shares": {"AAA": 300}},
                     conditions=conds, holdings=holdings, ref={"AAA": 10.0})
    protect = by_kind(draft, KIND_PROTECT)
    assert len(protect) == 1 and protect[0].planned_qty == 700
    assert any("缩减" in w for w in protect[0].warnings)


# ── A12 持仓政策 ──


def test_a12_policies_constrain_orders_and_observe_counts_capital():
    holdings = {
        "MAN": hold("MAN", 500, 10.0, POLICY_MANAGED),
        "EXIT": hold("EXIT", 400, 10.0, POLICY_EXIT_ONLY),
        "OBS": hold("OBS", 600, 10.0, POLICY_OBSERVE),
    }
    ref = {"MAN": 10.0, "EXIT": 10.0, "OBS": 10.0, "NEW": 10.0}
    config = ExecutionConfig(max_positions=3, commission_rate=0.0,
                             min_commission=0.0, stamp_tax_rate=0.0,
                             transfer_fee_rate=0.0)
    actions = {"sell": ["MAN", "EXIT", "OBS"], "buy": ["NEW"],
               "sell_shares": {"EXIT": 100}}
    draft = draft_of(actions, holdings=holdings, ref=ref,
                     cash_available=5000.0, config=config)
    sells = {o.symbol: o for o in by_kind(draft, KIND_SELL)}
    assert sells["MAN"].planned_qty == 500  # managed 全卖
    assert sells["EXIT"].planned_qty == 100  # exit_only 只许卖
    assert "OBS" not in sells  # observe 不可动
    assert by_kind(draft, KIND_BUY)  # 新标的可买
    assert any("observe" in w for w in draft.warnings)
    # observe 计入持仓容量：3 只持仓（含 1 只 observe）已满 max_positions=3，
    # 若 observe 不计数则不会触发容量提示
    assert any("max_positions" in w for w in draft.warnings)


def test_a12_exit_only_blocks_add_and_target_buy():
    holdings = {"EXIT": hold("EXIT", 400, 10.0, POLICY_EXIT_ONLY)}
    draft = draft_of({"target_value": {"EXIT": 8000.0}}, holdings=holdings,
                     ref={"EXIT": 10.0}, cash_available=10000.0)
    assert by_kind(draft, KIND_BUY) == []
    assert by_kind(draft, KIND_SELL) == []
    assert any("exit_only" in w for w in draft.warnings)


# ── A17 条件转换 ──


def test_a17_price_conditions_translate_with_params_preserved():
    holdings = {"AAA": hold("AAA", 1000, 10.0)}
    conds = {"AAA": [
        {"type": "STOP_LOSS", "price": 9.0},
        {"type": "TAKE_PROFIT", "price": 12.0},
        {"type": "TRAILING_TP", "price": 11.0},
    ]}
    draft = draft_of({}, conditions=conds, holdings=holdings,
                     ref={"AAA": 10.0})
    protects = by_kind(draft, KIND_PROTECT)
    assert [(o.reason, o.detail["trigger_price"]) for o in protects] == [
        ("STOP_LOSS", 9.0), ("TAKE_PROFIT", 12.0), ("TRAILING_TP", 11.0)]
    assert all(o.planned_qty == 1000 for o in protects)
    assert all(o.detail["exclusive_group"] == "AAA" for o in protects)
    assert any("互斥" in w for w in draft.warnings)


def test_a17_ml_exit_becomes_explicit_sell_with_model_params():
    holdings = {"AAA": hold("AAA", 800, 10.0)}
    conds = {"AAA": [{"type": "ML_EXIT", "model": "g", "score": 0.83}]}
    draft = draft_of({}, conditions=conds, holdings=holdings,
                     ref={"AAA": 10.0})
    sells = by_kind(draft, KIND_SELL)
    assert len(sells) == 1
    assert sells[0].reason == "ML_EXIT"
    assert sells[0].detail == {"model": "g", "score": 0.83}
    assert sells[0].planned_qty == 800


def test_a17_unsupported_condition_raises_with_params_not_dropped():
    holdings = {"AAA": hold("AAA", 1000, 10.0)}
    conds = {"AAA": [{"type": "MY_COND", "price": 9.5, "custom": {"x": 1}}]}
    with pytest.raises(UnsupportedConditionError) as exc:
        draft_of({}, conditions=conds, holdings=holdings, ref={"AAA": 10.0})
    message = str(exc.value)
    assert "MY_COND" in message and "custom" in message


def test_a17_buy_conditions_translate_and_validate():
    actions = {"buy_conditions": [
        {"symbol": "AAA", "type": "LIMIT_BUY", "price": 9.5, "value": 5000},
        {"symbol": "BBB", "type": "BREAKOUT_BUY", "price": 21.0,
         "shares": 300},
    ]}
    draft = draft_of(actions, ref={"AAA": 10.0, "BBB": 20.0},
                     cash_available=10000.0)
    entries = {o.symbol: o for o in by_kind(draft, KIND_ENTRY_COND)}
    assert entries["AAA"].planned_qty == 500  # 5000/9.5 → 500 股
    assert entries["AAA"].ref_price == 9.5
    assert entries["BBB"].planned_qty == 300
    assert entries["BBB"].ref_price == 21.0


def test_a17_unsupported_buy_condition_raises():
    actions = {"buy_conditions": [
        {"symbol": "AAA", "type": "MY_BUY", "price": 9.5, "value": 100}]}
    with pytest.raises(UnsupportedConditionError) as exc:
        draft_of(actions, ref={"AAA": 10.0})
    assert "MY_BUY" in str(exc.value)


# ── A14 接受幂等 / 版本检查 / supersede ──


def test_a14_accept_idempotent_same_token(ledger, store):
    draft = make_draft_for_store()
    plan1 = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                              signal_date="20240105", trade_date="20240108",
                              expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    plan2 = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                              signal_date="20240105", trade_date="20240108",
                              expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    assert plan1.plan_id == plan2.plan_id
    assert len(store.orders(plan_id=plan1.plan_id)) == 1
    assert store.outstanding_reserved() == APPROX(2000.0)


def test_a14_same_day_different_decision_conflicts(ledger, store):
    draft = make_draft_for_store()
    store.accept_plan(draft, activation_id=1, snapshot_id=1,
                      signal_date="20240105", trade_date="20240108",
                      expiry_date="20240108",
                      decision_hash=draft.fingerprint())
    other = draft_of({"buy": ["CCC"]}, ref={"CCC": 10.0},
                     cash_available=10000.0)
    with pytest.raises(PlanConflictError):
        store.accept_plan(other, activation_id=1, snapshot_id=1,
                          signal_date="20240105", trade_date="20240108",
                          expiry_date="20240108",
                          decision_hash=other.fingerprint())


def test_a14_stale_snapshot_rejected(ledger, store):
    draft = make_draft_for_store()
    with pytest.raises(PlanStateError) as exc:
        store.accept_plan(draft, activation_id=1, snapshot_id=99,
                          signal_date="20240105", trade_date="20240108",
                          expiry_date="20240108",
                          decision_hash=draft.fingerprint())
    assert "账户版本" in str(exc.value)


def test_supersede_keeps_inflight_orders(ledger, store):
    draft1 = make_draft_for_store({"buy": ["BBB", "CCC"]})
    plan1 = store.accept_plan(draft1, activation_id=1, snapshot_id=1,
                              signal_date="20240105", trade_date="20240108",
                              expiry_date="20240108",
                              decision_hash=draft1.fingerprint())
    submitted = [o for o in plan1.orders if o.symbol == "BBB"][0]
    store.record_submission(submitted.client_order_id)
    draft2 = make_draft_for_store({"buy": ["DDD"]})
    plan2 = store.accept_plan(draft2, activation_id=1, snapshot_id=1,
                              signal_date="20240108", trade_date="20240109",
                              expiry_date="20240109",
                              decision_hash=draft2.fingerprint())
    reloaded1 = store.plan(plan1.plan_id)
    assert reloaded1.status == PLAN_SUPERSEDED
    statuses = {o.symbol: o.status for o in reloaded1.orders}
    assert statuses["BBB"] == ORDER_SUBMITTED  # 在途订单不能当作从未执行
    assert statuses["CCC"] == ORDER_EXPIRED  # 未提交的过期并释放
    assert store.plan(plan2.plan_id).status == PLAN_ACTIVE
    # BBB 的预留仍在占用；CCC 的已释放
    reserved = {o.symbol: o.budget_reserved
                for o in store.orders(plan_id=plan1.plan_id)}
    assert reserved["BBB"] > 0 and reserved["CCC"] == 0.0


# ── A19 原子性与成交关联（A24） ──


def test_a19_overfill_rejected_atomically(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    order = plan.orders[0]
    store.record_submission(order.client_order_id)
    with pytest.raises(PlanValidationError) as exc:
        store.record_fill(order.client_order_id, date="20240108",
                          quantity=600, price=10.0, source="sim",
                          trade_id="f1")
    assert "超计划" in str(exc.value)
    # 整笔回滚：账本无成交、订单状态/数量不变、预留未释放
    assert ledger.fills() == []
    reloaded = store.order(order.client_order_id)
    assert reloaded.status == ORDER_SUBMITTED
    assert reloaded.filled_qty == 0
    assert reloaded.budget_reserved > 0


def test_a19_fill_before_submission_rejected(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    with pytest.raises(PlanStateError):
        store.record_fill(plan.orders[0].client_order_id, date="20240108",
                          quantity=100, price=10.0, source="sim",
                          trade_id="f1")
    assert ledger.fills() == []


def test_a24_linked_fill_updates_order_and_account_atomically(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                              signal_date="20240105", trade_date="20240108",
                              expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    order = plan.orders[0]
    store.record_submission(order.client_order_id)
    filled = store.record_fill(order.client_order_id, date="20240108",
                               quantity=200, price=10.0,
                               fee_commission=5.0, source="sim",
                               trade_id="f1")
    assert filled.status == ORDER_FILLED
    assert filled.avg_fill_price == APPROX(10.0)
    assert filled.budget_reserved == 0.0  # 终结释放预留
    assert store.outstanding_reserved() == 0.0
    # 同一组合事务内账本已有关联成交
    ledger_fills = ledger.fills(plan_id=plan.plan_id)
    assert len(ledger_fills) == 1
    assert ledger_fills[0].order_ref == order.client_order_id
    assert ledger_fills[0].external is False
    assert store.plan(plan.plan_id).status == PLAN_COMPLETED
    # 幂等：同 trade_id 再报被账本拒绝且订单不变（组合事务整体回滚）
    with pytest.raises(Exception):
        store.record_fill(order.client_order_id, date="20240108",
                          quantity=100, price=10.0, source="sim",
                          trade_id="f1")
    assert store.order(order.client_order_id).filled_qty == 200


def test_a24_external_fill_recorded_without_touching_plans(ledger, store):
    ledger.record_fill(date="20240108", symbol="XYZ", side="BUY",
                       quantity=200, price=5.0, source="manual",
                       trade_id="ext1", note="人工转入")
    fills = ledger.fills()
    assert len(fills) == 1
    # 无计划关联的成交推导为 external（计划层不猜 reason，不产生计划行）
    assert fills[0].external is True
    assert store.orders() == []
    assert store.plans() == []


def test_price_deviation_limit_rejects_fill(ledger, store):
    config = ExecutionConfig(max_positions=5, price_deviation_limit=0.05,
                             commission_rate=0.0, min_commission=0.0,
                             stamp_tax_rate=0.0, transfer_fee_rate=0.0)
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    order = plan.orders[0]
    store.record_submission(order.client_order_id)
    with pytest.raises(PlanValidationError) as exc:
        store.record_fill(order.client_order_id, date="20240108",
                          quantity=200, price=12.0, source="sim",
                          trade_id="f1", execution_config=config)
    assert "偏离" in str(exc.value)
    assert ledger.fills() == []


# ── A18 状态机与执行器 ──


def test_a18_partial_fill_then_cancel_remaining(ledger, store):
    draft = make_draft_for_store({"buy": ["BBB", "CCC"]})
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    order = [o for o in plan.orders if o.symbol == "BBB"][0]
    store.record_submission(order.client_order_id)
    partial = store.record_fill(order.client_order_id, date="20240108",
                                quantity=100, price=10.0, source="sim",
                                trade_id="f1")
    assert partial.status == ORDER_PARTIAL
    assert store.plan(plan.plan_id).status == PLAN_PARTIAL
    cancelled = store.record_cancellation(order.client_order_id,
                                          reason="剩余撤单")
    assert cancelled.status == ORDER_CANCELLED
    assert cancelled.filled_qty == 100
    assert cancelled.budget_reserved == 0.0


def test_a18_timeout_unknown_query_recovery_no_blind_resubmit(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    order = plan.orders[0]
    executor = SimulatedExecutor(script={
        order.client_order_id: {
            "status": "timeout",
            "query": {"status": "filled", "fills": [
                {"quantity": 200, "price": 10.05}]}}})
    outcome = executor.submit(order)
    assert outcome.status == ORDER_UNKNOWN
    results = apply_execution(store, outcome, source="sim")
    assert results[-1].status == ORDER_UNKNOWN
    unknown = store.order(order.client_order_id)
    assert unknown.status == ORDER_UNKNOWN
    # unknown 不能盲目重发：record_submission 拒绝
    with pytest.raises(PlanStateError):
        store.record_submission(order.client_order_id)
    # 查询核验恢复：成交回报入账
    query = executor.query(store.order(order.client_order_id))
    assert query.status == ORDER_FILLED
    apply_execution(store, query, source="sim",
                    trade_date=plan.trade_date)
    resolved = store.order(order.client_order_id)
    assert resolved.status == ORDER_FILLED
    assert resolved.filled_qty == 200
    assert resolved.avg_fill_price == APPROX(10.05)
    assert len(ledger.fills(plan_id=plan.plan_id)) == 1


def test_a18_repeated_submit_does_not_duplicate(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    order = plan.orders[0]
    executor = ManualExecutor()
    apply_execution(store, executor.submit(order), source="manual")
    apply_execution(store, executor.submit(store.order(
        order.client_order_id)), source="manual")
    assert len(store.orders(plan_id=plan.plan_id)) == 1
    assert store.order(order.client_order_id).status == ORDER_SUBMITTED


def test_a18_simulator_reject_and_multi_fill(ledger, store):
    draft = make_draft_for_store({"buy": ["BBB", "CCC"]})
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    orders = {o.symbol: o for o in plan.orders}
    executor = SimulatedExecutor(script={
        orders["BBB"].client_order_id: {"status": "rejected",
                                        "reason": "涨停拒单"},
        orders["CCC"].client_order_id: {"status": "partial", "fills": [
            {"quantity": 120, "price": 10.0},
            {"quantity": 60, "price": 10.02}]}})
    apply_execution(store, executor.submit(orders["BBB"]), source="sim")
    rejected = store.order(orders["BBB"].client_order_id)
    assert rejected.status == ORDER_REJECTED
    assert rejected.budget_reserved == 0.0
    apply_execution(store, executor.submit(orders["CCC"]), source="sim")
    partial = store.order(orders["CCC"].client_order_id)
    assert partial.status == ORDER_PARTIAL
    assert partial.filled_qty == 180
    assert partial.avg_fill_price == APPROX((120 * 10.0 + 60 * 10.02) / 180)


def test_manual_executor_instruction_sheet(ledger, store):
    draft = make_draft_for_store({"sell": ["AAA"], "buy": ["BBB"]})
    draft = build_plan(
        {"sell": ["AAA"], "buy": ["BBB"]}, {},
        view({"AAA": hold("AAA", 1000, 10.0)}, {"AAA": 10.0, "BBB": 10.0},
             20000.0), budget(10000.0), ZERO_FEES)
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    rows = ManualExecutor().instruction_sheet(plan)
    assert {r["action"] for r in rows} == {"买入", "卖出"}
    assert all(r["client_order_id"].startswith(plan.plan_id) for r in rows)
    assert all("可卖数量" in n for r in rows for n in r["notes"][-1:])
    with pytest.raises(PlanStateError):
        ManualExecutor().query(plan.orders[0])


# ── 生命周期：跳过 / 过期 / 取消 ──


def test_a19_duplicate_fill_report_is_idempotent_noop(ledger, store):
    """LIVE-V2-02：同一 partial 订单重复回报同笔成交（同 trade_id 同内容）
    必须整体 no-op：订单 filled_qty 不变、账本仅 1 行、现金只扣一次。"""
    draft = make_draft_for_store({"buy": ["BBB", "CCC"]})
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    order = [o for o in plan.orders if o.symbol == "BBB"][0]
    store.record_submission(order.client_order_id)
    first = store.record_fill(order.client_order_id, date="20240108",
                              quantity=100, price=10.0, source="sim",
                              trade_id="T1")
    assert first.status == ORDER_PARTIAL and first.filled_qty == 100
    repeated = store.record_fill(order.client_order_id, date="20240108",
                                 quantity=100, price=10.0, source="sim",
                                 trade_id="T1")
    assert repeated.filled_qty == 100 and repeated.status == ORDER_PARTIAL
    assert repeated.budget_reserved > 0  # 预留未被重复回报释放
    assert len(ledger.fills()) == 1
    assert ledger.projection().cash == pytest.approx(10000 - 1000)


@pytest.mark.parametrize("date,price", [("20240108", 11.0), ("20240109", 10.0)])
def test_a19_same_trade_id_different_content_rejected(ledger, store, date, price):
    """同 trade_id 异内容不是幂等：必须拒绝且订单/账本均不变。"""
    draft = make_draft_for_store({"buy": ["BBB", "CCC"]})
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    order = [o for o in plan.orders if o.symbol == "BBB"][0]
    store.record_submission(order.client_order_id)
    store.record_fill(order.client_order_id, date="20240108", quantity=100,
                      price=10.0, source="sim", trade_id="T1")
    with pytest.raises(Exception):
        store.record_fill(order.client_order_id, date=date,
                          quantity=100, price=price, source="sim",
                          trade_id="T1")
    assert store.order(order.client_order_id).filled_qty == 100
    assert len(ledger.fills()) == 1


def test_reaccept_after_cancel_is_clean_state_error(ledger, store):
    """LIVE-V2-03：取消后同日重接受不能裸堆栈，应返回稳定 state 错误。"""
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                             decision_hash=draft.fingerprint())
    store.cancel_plan(plan.plan_id, reason="改主意")
    with pytest.raises(PlanStateError) as exc:
        store.accept_plan(draft, activation_id=1, snapshot_id=1,
                          signal_date="20240105", trade_date="20240108",
                          expiry_date="20240108",
                          decision_hash=draft.fingerprint())
    assert "cancelled" in str(exc.value) or "重建" in str(exc.value)


def test_skip_requires_reason_and_idempotent(ledger, store):
    with pytest.raises(Exception):
        store.skip_decision(activation_id=1, snapshot_id=1,
                            signal_date="20240105", decision_hash="h",
                            reason="")
    plan = store.skip_decision(activation_id=1, snapshot_id=1,
                               signal_date="20240105", decision_hash="h",
                               reason="人工判断不调仓")
    assert plan.status == PLAN_SKIPPED and plan.skip_reason == "人工判断不调仓"
    again = store.skip_decision(activation_id=1, snapshot_id=1,
                                signal_date="20240105", decision_hash="h",
                                reason="人工判断不调仓")
    assert again.plan_id == plan.plan_id
    # 同日不能再接受不同记录
    draft = make_draft_for_store()
    with pytest.raises(PlanConflictError):
        store.accept_plan(draft, activation_id=1, snapshot_id=1,
                          signal_date="20240105", trade_date="20240108",
                          expiry_date="20240108",
                          decision_hash=draft.fingerprint())


def test_expire_releases_planned_reservations(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    assert store.outstanding_reserved() > 0
    expired = store.expire_plans("20240109")
    assert [p.plan_id for p in expired] == [plan.plan_id]
    assert store.plan(plan.plan_id).status == PLAN_EXPIRED
    assert store.outstanding_reserved() == 0.0
    assert store.order(plan.orders[0].client_order_id).status == ORDER_EXPIRED


def test_cancel_blocked_while_in_flight(ledger, store):
    draft = make_draft_for_store()
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108",
                              decision_hash=draft.fingerprint())
    store.record_submission(plan.orders[0].client_order_id)
    with pytest.raises(PlanStateError) as exc:
        store.cancel_plan(plan.plan_id, reason="改主意")
    assert "在途" in str(exc.value)
    store.record_cancellation(plan.orders[0].client_order_id, reason="撤单")
    # 全部订单终结且无成交 → 计划自动收敛为 cancelled
    assert store.plan(plan.plan_id).status == PLAN_CANCELLED


# ── 配置与输入校验 ──


def test_execution_config_validation():
    with pytest.raises(PlanValidationError):
        ExecutionConfig(max_positions=0)
    with pytest.raises(PlanValidationError):
        ExecutionConfig(cash_buffer=-1.0)
    with pytest.raises(PlanValidationError):
        ExecutionConfig(price_deviation_limit=0.0)
    with pytest.raises(PlanValidationError):
        ExecutionConfig(min_trade_amount=float("nan"))


def test_accept_validates_dates_and_budget(ledger, store):
    draft = make_draft_for_store()
    with pytest.raises(PlanValidationError):
        store.accept_plan(draft, activation_id=1, snapshot_id=1,
                          signal_date="20240108", trade_date="20240105",
                          expiry_date="20240109",
                          decision_hash=draft.fingerprint())
    with pytest.raises(PlanValidationError):
        store.accept_plan(draft, activation_id=1, snapshot_id=1,
                          signal_date="20240108", trade_date="20240109",
                          expiry_date="bad-date",
                          decision_hash=draft.fingerprint())
    # 超出当前可用现金的草稿被拒（不重复预留）：先落一笔真实预留
    draft0 = make_draft_for_store({"buy": ["BBB"]})
    store.accept_plan(draft0, activation_id=1, snapshot_id=1,
                      signal_date="20240104", trade_date="20240105",
                      expiry_date="20240105",
                      decision_hash=draft0.fingerprint())
    # 账本真实可用 10000 - 2000 = 8000；新草稿需预留 9000 → 拒绝
    big = draft_of({"buy": ["BBB"]}, ref={"BBB": 10.0},
                   cash_available=45000.0, config=ZERO_FEES)
    assert sum(o.budget_reserved for o in big.orders) == APPROX(9000.0)
    with pytest.raises(PlanStateError):
        store.accept_plan(big, activation_id=1, snapshot_id=1,
                          signal_date="20240105", trade_date="20240108",
                          expiry_date="20240108",
                          decision_hash=big.fingerprint())
