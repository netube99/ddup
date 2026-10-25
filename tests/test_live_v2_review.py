"""最后提交实盘 V2 的独立回归；全部使用临时账本与手算期望。"""

import pytest

from research.live import import_statement
from research.live_account import AccountLedger, AccountValidationError
from research.live_plan import (
    AccountView,
    BudgetContext,
    ExecutionConfig,
    HoldingView,
    PlanStore,
    build_plan,
    gate_order,
)


@pytest.fixture()
def ledger(tmp_path):
    with AccountLedger.create(str(tmp_path / "review.duckdb"), account_id="review") as led:
        yield led


@pytest.mark.parametrize("opening", [True, False])
def test_statement_failure_rolls_back_all_facts(ledger, opening):
    if not opening:
        import_statement(ledger, {"date": "20240105", "cash": 10000, "holdings": []})
    before = ledger.status().counts.copy()
    with pytest.raises(AccountValidationError):
        import_statement(ledger, {
            "date": "20240108", "cash": 11000, "holdings": [],
            "cash_flows": [{"category": "deposit", "amount": 1000}],
            "fills": [{"symbol": "AAA", "side": "BUY", "quantity": 0,
                       "price": 10, "trade_id": "bad"}],
        })
    assert ledger.status().counts == before
    assert ledger.cash_flows() == []
    if opening:
        assert ledger.confirmed_snapshot() is None
    else:
        assert ledger.projection().cash == 10000


@pytest.mark.parametrize("field,value", [
    ("shares", 100.5), ("available", 99.5), ("shares", True), ("cash", True),
    ("delta_shares", 1.5), ("delta_shares", True),
])
def test_statement_rejects_lossy_numeric_coercion(ledger, field, value):
    payload = {"date": "20240105", "cash": 10000,
               "holdings": [{"symbol": "AAA", "shares": 100, "available": 100}]}
    if field == "cash":
        payload[field] = value
    elif field == "delta_shares":
        payload["share_adjustments"] = [{"symbol": "AAA", "delta_shares": value,
                                         "category": "corporate_action", "reason": "送股"}]
    else:
        payload["holdings"][0][field] = value
        if field == "shares":
            payload["holdings"][0].pop("available")
    with pytest.raises(AccountValidationError):
        import_statement(ledger, payload)
    assert ledger.confirmed_snapshot() is None


@pytest.mark.parametrize("buy_price,buy_fee,final_quantity", [
    (10, 0, 100), (10, 20, 0), (11, 0, 0),
])
def test_dependency_budget_is_consumed_by_partial_buy(
    ledger, buy_price, buy_fee, final_quantity,
):
    ledger.import_snapshot(as_of="20240105", source="broker", import_id="s1",
                           cash=1000, holdings=[{"symbol": "AAA", "shares": 1000}])
    store = PlanStore.attach(ledger)
    config = ExecutionConfig(max_positions=5, commission_rate=0, min_commission=0,
                             stamp_tax_rate=0, transfer_fee_rate=0)
    account = AccountView(snapshot_id=1, base_as_of="20240105", total_value=11000,
                          holdings={"AAA": HoldingView("AAA", 1000, 10000, "managed")},
                          ref_prices={"AAA": 10, "BBB": 10})
    draft = build_plan({"sell": ["AAA"], "buy": ["BBB"]}, {}, account,
                       BudgetContext(1000, 0), config)
    plan = store.accept_plan(draft, activation_id=1, snapshot_id=1,
                             signal_date="20240105", trade_date="20240108",
                             expiry_date="20240108", decision_hash=draft.fingerprint())
    sell, buy = plan.orders
    store.record_submission(sell.client_order_id)
    store.record_fill(sell.client_order_id, date="20240108", quantity=400,
                      price=10, source="broker", trade_id="sell1")
    assert gate_order(store, buy.client_order_id).quantity == 100
    store.record_submission(buy.client_order_id)
    store.record_fill(buy.client_order_id, date="20240108", quantity=100,
                      price=buy_price, fee_commission=buy_fee,
                      source="broker", trade_id="buy1")
    # 现金预留1000 + 分配回款1000*40% - 已买100*10 = 400，不足一手。
    gate = gate_order(store, buy.client_order_id)
    assert not gate.allowed and gate.quantity == 0
    store.record_fill(sell.client_order_id, date="20240108", quantity=600,
                      price=10, source="broker", trade_id="sell2")
    assert gate_order(store, buy.client_order_id).quantity == final_quantity
