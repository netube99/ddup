"""实盘 V2 CLI 集成测试 — 每日闭环与错误通道（docs/live_redesign.md §6）。

策略会话命令用仓库 fixtures 行情离线运行（--fixtures-dir），不依赖
本机真实行情库；账户/计划命令不需要行情。全部经 scripts/live.main
驱动，独立期望来自手算现金/股数与协议约定。
"""

import json

import pytest
import yaml

import scripts.live as live_cli
from research.live_account import AccountLedger

FIXTURES = "tests/fixtures"
YAML_STRATEGY = "strategies/examples/rolling_ranker/config.yaml"
APPROX = pytest.approx


def run_cli(capsys, *argv):
    rc = live_cli.main([str(a) for a in argv])
    out = capsys.readouterr().out
    assert out.strip().startswith("{"), f"non-JSON output: {out[:200]}"
    return rc, json.loads(out)


def write_yaml(tmp_path, name, payload):
    path = tmp_path / name
    path.write_text(yaml.safe_dump(payload, allow_unicode=True,
                                   sort_keys=False), encoding="utf-8")
    return str(path)


@pytest.fixture()
def db(tmp_path, capsys):
    path = str(tmp_path / "main.duckdb")
    live_cli.main(["init", path, "--account-id", "main", "--broker", "test"])
    capsys.readouterr()  # 排空 init 输出，避免污染后续读取
    return path


def statement(tmp_path, *, date, cash, holdings, fills=None, cash_flows=None,
              import_id=None):
    payload = {"date": date, "cash": cash, "holdings": holdings}
    if fills:
        payload["fills"] = fills
    if cash_flows:
        payload["cash_flows"] = cash_flows
    if import_id:
        payload["import_id"] = import_id
    return write_yaml(tmp_path, f"stmt_{date}.yaml", payload)


# ── 建账与状态 ──


def test_init_duplicate_refused_and_missing_status_errors(tmp_path, capsys):
    path = str(tmp_path / "a.duckdb")
    rc, out = run_cli(capsys, "init", path, "--account-id", "a")
    assert rc == 0 and out["ok"]
    rc, out = run_cli(capsys, "init", path, "--account-id", "a")
    assert rc == 1 and out["ok"] is False and out["code"] == "conflict"
    assert not (tmp_path / "missing.duckdb").exists()
    rc, out = run_cli(capsys, "status", str(tmp_path / "missing.duckdb"))
    assert rc == 1 and out["code"] == "not_found"


def test_import_clean_confirms_and_status_fresh(tmp_path, capsys, db):
    """A02：sync/import 成功后 status 立即反映新状态，无需运行策略。"""
    stmt = statement(tmp_path, date="20240603", cash=100000, holdings=[
        {"symbol": "000001.SZ", "shares": 1000, "available": 1000}])
    rc, out = run_cli(capsys, "import", db, stmt)
    assert rc == 0 and out["ok"]
    assert out["snapshot"]["action"] == "opening_confirmed"
    rc, out = run_cli(capsys, "status", db, "--json")
    assert rc == 0
    assert out["confirmed"]["as_of"] == "20240603"
    assert out["confirmed"]["cash_balance"] == 100000
    assert out["projection"]["cash"] == 100000
    assert out["pending_candidates"] == []


def test_import_idempotent_rerun(tmp_path, capsys, db):
    stmt = statement(tmp_path, date="20240603", cash=100000, holdings=[])
    rc, first = run_cli(capsys, "import", db, stmt)
    assert rc == 0
    rc, second = run_cli(capsys, "import", db, stmt)
    assert rc == 0 and second["snapshot"]["action"] == "idempotent_noop"
    ledger = AccountLedger.open(db)
    try:
        assert ledger.status().counts["snapshots"] == 1
    finally:
        ledger.close()


def test_identical_fills_different_trade_id_both_kept(tmp_path, capsys, db):
    """A03：内容完全相同的两笔真实成交（不同 trade_id）都保留。"""
    stmt = statement(tmp_path, date="20240603", cash=2000, holdings=[
        {"symbol": "000001.SZ", "shares": 200}], fills=[
        {"symbol": "000001.SZ", "side": "BUY", "quantity": 100,
         "price": 10.0, "trade_id": "T1"},
        {"symbol": "000001.SZ", "side": "BUY", "quantity": 100,
         "price": 10.0, "trade_id": "T2"},
    ], cash_flows=[{"category": "deposit", "amount": 1000}])
    rc, out = run_cli(capsys, "import", db, stmt)
    assert rc == 0 and out["fills"]["appended"] == 2
    ledger = AccountLedger.open(db)
    try:
        fills = ledger.fills()
        assert [f.trade_id for f in fills] == ["T1", "T2"]
        assert all(f.quantity == 100 for f in fills)
    finally:
        ledger.close()


def test_deposit_is_not_investment_pnl(tmp_path, capsys, db):
    """A04：入金改变现金但不产生投资盈亏。"""
    stmt = statement(tmp_path, date="20240603", cash=10000, holdings=[])
    run_cli(capsys, "import", db, stmt)
    rc, out = run_cli(capsys, "cashflow", db, "--category", "deposit",
                      "--amount", "1000", "--date", "20240604")
    assert rc == 0 and out["ok"]
    stmt2 = statement(tmp_path, date="20240604", cash=11000, holdings=[])
    rc, out = run_cli(capsys, "import", db, stmt2)
    assert rc == 0 and out["snapshot"]["action"] == "imported_confirmed"
    ledger = AccountLedger.open(db)
    try:
        pnl = ledger.investment_pnl()
        assert pnl.complete is True
        assert pnl.investment_pnl == APPROX(0.0)
        assert ledger.projection().cash == APPROX(11000.0)
    finally:
        ledger.close()


def test_candidate_diff_blocks_until_classified(tmp_path, capsys, db):
    """A06：不明差异存候选；分类事实后 confirm 通过；期间 status 可见。"""
    run_cli(capsys, "import", db, statement(
        tmp_path, date="20240603", cash=10000, holdings=[]))
    # 未入账的 500 元分红 → 候选
    stmt = statement(tmp_path, date="20240606", cash=10500, holdings=[])
    rc, out = run_cli(capsys, "import", db, stmt)
    assert rc == 0 and out["snapshot"]["action"] == "stored_candidate"
    assert out["pending_diffs"]["cash"]["delta"] == APPROX(500.0)
    rc, status = run_cli(capsys, "status", db, "--json")
    assert len(status["pending_candidates"]) == 1
    snapshot_id = status["pending_candidates"][0]["snapshot_id"]
    # 未解释前 confirm 仍 pending
    rc, out = run_cli(capsys, "confirm", db, "--snapshot", snapshot_id)
    assert rc == 0 and out["confirmed"] is False
    # 分类为分红后确认
    rc, out = run_cli(capsys, "cashflow", db, "--category", "dividend",
                      "--amount", "500", "--date", "20240606")
    assert rc == 0
    rc, out = run_cli(capsys, "confirm", db, "--snapshot", snapshot_id)
    assert rc == 0 and out["confirmed"] is True
    rc, status = run_cli(capsys, "status", db, "--json")
    assert status["confirmed"]["cash_balance"] == 10500
    assert status["pending_candidates"] == []


def test_takeover_leaves_pnl_incomplete_but_tradable(tmp_path, capsys, db):
    run_cli(capsys, "import", db, statement(
        tmp_path, date="20240603", cash=10000, holdings=[]))
    stmt = statement(tmp_path, date="20240606", cash=10000, holdings=[
        {"symbol": "000001.SZ", "shares": 300, "available": 300}])
    rc, out = run_cli(capsys, "import", db, stmt)
    assert out["snapshot"]["action"] == "stored_candidate"
    snapshot_id = out["snapshot"]["snapshot_id"]
    rc, out = run_cli(capsys, "confirm", db, "--snapshot", snapshot_id,
                      "--takeover", "--reason", "历史不完整的转入持仓")
    assert rc == 0 and out["action"] == "takeover_confirmed"
    ledger = AccountLedger.open(db)
    try:
        pnl = ledger.investment_pnl(closing_prices={"000001.SZ": 10.0})
        assert pnl.complete is False
        assert any("takeover_gap" in r for r in pnl.reasons)
        # 显式接管后可继续交易：预算不被封锁
        from research.live_plan import PlanStore
        store = PlanStore.attach(ledger)
        assert store.budget_context().blocked_reason is None
    finally:
        ledger.close()


# ── 政策 ──


def test_policy_managed_requires_anchor(tmp_path, capsys, db):
    run_cli(capsys, "import", db, statement(
        tmp_path, date="20240603", cash=10000, holdings=[
            {"symbol": "000001.SZ", "shares": 500, "available": 500}]))
    rc, out = run_cli(capsys, "policy", db, "--symbol", "000001.SZ",
                      "--policy", "managed")
    assert rc == 2 and out["ok"] is False
    rc, out = run_cli(capsys, "policy", db, "--symbol", "000001.SZ",
                      "--policy", "managed", "--anchor-date", "20240520",
                      "--anchor-price", "9.8", "--reason", "接管")
    assert rc == 0 and out["policy"] == "managed"
    rc, out = run_cli(capsys, "policy", db, "--symbol", "000001.SZ",
                      "--policy", "exit_only", "--reason", "只管退出")
    assert rc == 0 and out["policy"] == "exit_only"
    rc, status = run_cli(capsys, "status", db, "--json")
    assert status["policies"]["000001.SZ"] == "exit_only"


# ── 策略会话闭环 ──


def _activated(tmp_path, capsys, db):
    run_cli(capsys, "import", db, statement(
        tmp_path, date="20240603", cash=100000, holdings=[
            {"symbol": "000001.SZ", "shares": 1000, "available": 1000}]))
    anchors = write_yaml(tmp_path, "anchors.yaml", {"anchors": {
        "000001.SZ": {"entry_date": "20240506", "entry_price": 10.0}}})
    rc, out = run_cli(capsys, "activate", db, "--date", "20240603",
                      "--yaml", YAML_STRATEGY, "--anchors", anchors,
                      "--fixtures-dir", FIXTURES)
    assert rc == 0 and out["ok"]
    return out


def test_activate_preview_accept_flow(tmp_path, capsys, db):
    _activated(tmp_path, capsys, db)
    rc, preview = run_cli(capsys, "preview", db, "--date", "20240603",
                          "--fixtures-dir", FIXTURES, "--json")
    assert rc == 0 and preview["ok"]
    assert preview["decision_hash"] and preview["trade_date"] == "20240604"
    assert preview["instruction_sheet"]
    rc, plan = run_cli(capsys, "accept", db, "--date", "20240603",
                       "--fixtures-dir", FIXTURES)
    assert rc == 0 and plan["ok"] and plan["orders"]
    plan_id = plan["plan_id"]
    # 同快照立即重复接受：预览被本计划自身预留改变 → 明确拒绝不双倍预留
    rc, again = run_cli(capsys, "accept", db, "--date", "20240603",
                        "--fixtures-dir", FIXTURES)
    assert rc == 1 and again["code"] == "conflict"
    rc, status = run_cli(capsys, "status", db, "--with-session", "--json",
                         "--fixtures-dir", FIXTURES)
    assert status["outstanding_reserved"] > 0
    assert status["activation"]["strategy"]
    rc, plans = run_cli(capsys, "plans", db)
    assert {p["plan_id"] for p in plans["plans"]} == {plan_id}


def test_accept_after_sync_same_day_is_idempotent(tmp_path, capsys, db):
    """A14：接受后快照推进，同日重复接受返回既有计划，不双倍预留。"""
    _activated(tmp_path, capsys, db)
    rc, plan = run_cli(capsys, "accept", db, "--date", "20240603",
                       "--fixtures-dir", FIXTURES)
    assert rc == 0 and plan["ok"]
    run_cli(capsys, "import", db, statement(
        tmp_path, date="20240604", cash=100000, holdings=[
            {"symbol": "000001.SZ", "shares": 1000, "available": 1000}]))
    rc, again = run_cli(capsys, "accept", db, "--date", "20240603",
                        "--fixtures-dir", FIXTURES)
    assert rc == 0 and again["plan_id"] == plan["plan_id"]
    ledger = AccountLedger.open(db)
    try:
        from research.live_plan import PlanStore
        store = PlanStore.attach(ledger)
        assert len(store.plans()) == 1
    finally:
        ledger.close()


def test_skip_records_reason_and_conflicts_with_accept(tmp_path, capsys, db):
    _activated(tmp_path, capsys, db)
    rc, out = run_cli(capsys, "skip", db, "--date", "20240603",
                      "--reason", "人工判断不调仓", "--fixtures-dir", FIXTURES)
    assert rc == 0 and out["status"] == "skipped"
    rc, out = run_cli(capsys, "skip", db, "--date", "20240603",
                      "--reason", "人工判断不调仓", "--fixtures-dir", FIXTURES)
    assert rc == 0 and out["plan_id"]  # 幂等
    rc, out = run_cli(capsys, "accept", db, "--date", "20240603",
                      "--fixtures-dir", FIXTURES)
    assert rc == 1 and out["code"] == "conflict"


def test_execution_lifecycle_via_cli(tmp_path, capsys, db):
    """A18/A24：submit → 部分成交 → 撤单；超计划回报整笔回滚。"""
    _activated(tmp_path, capsys, db)
    rc, plan = run_cli(capsys, "accept", db, "--date", "20240603",
                       "--fixtures-dir", FIXTURES)
    order = next(o for o in plan["orders"] if o["kind"] == "BUY")
    oid = order["client_order_id"]
    rc, out = run_cli(capsys, "fill", db, "--order", oid,
                      "--date", "20240604", "--qty", order["planned_qty"],
                      "--price", order["ref_price"])
    assert rc == 1 and out["code"] == "state"  # 未提交先回报 → 拒绝
    rc, out = run_cli(capsys, "submit", db, "--order", oid)
    assert rc == 0 and out["status"] == "submitted"
    half = order["planned_qty"] // 2 // 100 * 100
    rc, out = run_cli(capsys, "fill", db, "--order", oid,
                      "--date", "20240604", "--qty", half,
                      "--price", order["ref_price"])
    assert rc == 0 and out["status"] == "partial"
    rc, out = run_cli(capsys, "fill", db, "--order", oid,
                      "--date", "20240604", "--qty", order["planned_qty"],
                      "--price", order["ref_price"])
    assert rc == 1 and "超计划" in out["message"]  # 整笔回滚
    rc, out = run_cli(capsys, "cancel-order", db, "--order", oid,
                      "--reason", "剩余撤单")
    assert rc == 0 and out["status"] == "cancelled"
    assert out["filled_qty"] == half
    rc, orders = run_cli(capsys, "orders", db, "--plan", plan["plan_id"])
    cancelled = next(o for o in orders["orders"]
                     if o["client_order_id"] == oid)
    assert cancelled["budget_reserved"] == 0.0
    ledger = AccountLedger.open(db)
    try:
        from research.live_plan import PlanStore
        store = PlanStore.attach(ledger)
        others = sum(o.budget_reserved for o in store.orders()
                     if o.client_order_id != oid)
        assert store.outstanding_reserved() == APPROX(others)
        linked = ledger.fills(plan_id=plan["plan_id"])
        assert len(linked) == 1 and linked[0].quantity == half
    finally:
        ledger.close()


def test_unknown_requires_resolve_no_blind_resubmit(tmp_path, capsys, db):
    _activated(tmp_path, capsys, db)
    rc, plan = run_cli(capsys, "accept", db, "--date", "20240603",
                       "--fixtures-dir", FIXTURES)
    order = next(o for o in plan["orders"] if o["kind"] == "BUY")
    oid = order["client_order_id"]
    run_cli(capsys, "submit", db, "--order", oid)
    rc, out = run_cli(capsys, "unknown", db, "--order", oid,
                      "--note", "券商超时")
    assert rc == 0 and out["status"] == "unknown"
    rc, out = run_cli(capsys, "submit", db, "--order", oid)
    assert rc == 1  # unknown 不能盲目重发
    fills = write_yaml(tmp_path, "fills.yaml", {"fills": [
        {"quantity": order["planned_qty"], "price": order["ref_price"],
         "date": "20240604"}]})
    rc, out = run_cli(capsys, "resolve", db, "--order", oid,
                      "--status", "filled", "--fills", fills)
    assert rc == 0 and out["status"] == "filled"
    assert out["filled_qty"] == order["planned_qty"]


def test_preview_human_table_and_trial(tmp_path, capsys, db):
    _activated(tmp_path, capsys, db)
    rc = live_cli.main(["preview", db, "--date", "20240603",
                        "--fixtures-dir", FIXTURES])
    out = capsys.readouterr().out
    assert rc == 0
    assert "信号日 20240603" in out and "操作单" in out
    rc = live_cli.main(["preview", db, "--date", "20240603",
                        "--fixtures-dir", FIXTURES, "--json",
                        "--trial-yaml", YAML_STRATEGY])
    trial = json.loads(capsys.readouterr().out)
    assert rc == 0 and trial["is_trial"] is True


# ── 错误通道 ──


def test_import_fills_csv_and_duplicate_report( tmp_path, capsys, db):
    """LIVE-V2-01/02：规范 CSV 成交导入；重复回报幂等；缺省成交日落库断言。"""
    run_cli(capsys, "import", db, statement(
        tmp_path, date="20240603", cash=5000, holdings=[
            {"symbol": "000001.SZ", "shares": 200, "available": 200}]))
    csv_path = tmp_path / "fills.csv"
    csv_path.write_text(
        "trade_id,date,symbol,side,quantity,price,fee_commission\n"
        "T1,,000001.SZ,SELL,100,11.0,1.0\n"
        "T2,20240604,000001.SZ,SELL,0,,\n",
        encoding="utf-8")
    rc, out = run_cli(capsys, "import-fills", db, str(csv_path),
                      "--date", "20240604")
    # 第 2 行 quantity=0 非法 → 整批拒绝且不落库
    assert rc == 1 and out["code"] == "validation"
    csv_path.write_text(
        "trade_id,date,symbol,side,quantity,price,fee_commission\n"
        "T1,,000001.SZ,SELL,100,11.0,1.0\n"
        "T2,20240605,000001.SZ,SELL,50,11.5,0.5\n",
        encoding="utf-8")
    rc, out = run_cli(capsys, "import-fills", db, str(csv_path),
                      "--date", "20240604")
    assert rc == 0 and out["appended"] == 2
    ledger = AccountLedger.open(db)
    try:
        fills = ledger.fills()
        # LIVE-V2-05：CSV 缺 date 行落库日期 = --date 缺省日
        assert [(f.trade_id, f.date) for f in fills] == [
            ("T1", "20240604"), ("T2", "20240605")]
    finally:
        ledger.close()
    rc, out = run_cli(capsys, "import-fills", db, str(csv_path),
                      "--date", "20240604")
    assert rc == 0 and out["appended"] == 0 and out["skipped"] == 2


def test_statement_fill_default_date_persisted(tmp_path, capsys, db):
    """LIVE-V2-05：statement fills 不带 date → 落库日期 = statement 日期。"""
    stmt = statement(tmp_path, date="20240604", cash=5000, holdings=[
        {"symbol": "000001.SZ", "shares": 100, "available": 100}], fills=[
        {"symbol": "000001.SZ", "side": "SELL", "quantity": 100,
         "price": 11.0, "trade_id": "S1"}])
    rc, out = run_cli(capsys, "import", db, stmt)
    assert rc == 0
    ledger = AccountLedger.open(db)
    try:
        assert ledger.fills()[0].date == "20240604"
    finally:
        ledger.close()


def test_duplicate_fill_report_via_cli_is_noop(tmp_path, capsys, db):
    """LIVE-V2-02 CLI：重复 fill 回报不改订单状态也不双记账。"""
    _activated(tmp_path, capsys, db)
    rc, plan = run_cli(capsys, "accept", db, "--date", "20240603",
                       "--fixtures-dir", FIXTURES)
    order = next(o for o in plan["orders"] if o["kind"] == "BUY")
    oid = order["client_order_id"]
    run_cli(capsys, "submit", db, "--order", oid)
    args = ["fill", db, "--order", oid, "--date", "20240604",
            "--qty", str(order["planned_qty"]),
            "--price", str(order["ref_price"]), "--trade-id", "B1"]
    rc, first = run_cli(capsys, *args)
    assert rc == 0 and first["status"] == "filled"
    rc, repeat = run_cli(capsys, *args)
    assert rc == 0 and repeat["status"] == "filled"
    ledger = AccountLedger.open(db)
    try:
        linked = ledger.fills(plan_id=plan["plan_id"])
        assert len(linked) == 1
    finally:
        ledger.close()


def test_cancel_plan_then_reaccept_clean_error(tmp_path, capsys, db):
    """LIVE-V2-03 CLI：取消后同日重接受返回稳定 state 错误而非堆栈。"""
    _activated(tmp_path, capsys, db)
    rc, plan = run_cli(capsys, "accept", db, "--date", "20240603",
                       "--fixtures-dir", FIXTURES)
    rc, out = run_cli(capsys, "cancel-plan", db, "--plan", plan["plan_id"],
                      "--reason", "改主意")
    assert rc == 0
    rc, out = run_cli(capsys, "accept", db, "--date", "20240603",
                      "--fixtures-dir", FIXTURES)
    assert rc == 1 and out["code"] == "state"
    assert "不能重建" in out["message"]


def test_error_channels(tmp_path, capsys, db):
    rc, out = run_cli(capsys, "import", db, str(tmp_path / "nope.yaml"))
    assert rc == 2 and out["code"] == "validation"
    rc, out = run_cli(capsys, "import", db, write_yaml(
        tmp_path, "bad.yaml", {"date": "20240603", "cash": 100,
                               "holdings": [{"symbol": "X"}]}))
    assert rc == 1 and out["code"] == "validation"
    rc, out = run_cli(capsys, "activate", db, "--date", "20240603",
                      "--yaml", "no/such.yaml", "--fixtures-dir", FIXTURES)
    assert rc == 2 and out["code"] == "validation"
    rc, out = run_cli(capsys, "preview", db, "--fixtures-dir", FIXTURES,
                      "--json")
    assert rc == 1 and out["code"] == "state"  # 未启用策略
    # 旧 V1 账本（有 ledger_meta 无 acct 表）明确拒绝且不写坏文件
    import duckdb

    old_db = str(tmp_path / "v1.duckdb")
    conn = duckdb.connect(old_db)
    conn.execute("CREATE TABLE ledger_meta (key VARCHAR, value VARCHAR)")
    conn.execute("INSERT INTO ledger_meta VALUES"
                 " ('schema_version', '1'), ('initial_capital', '1000')")
    conn.close()
    rc, out = run_cli(capsys, "status", old_db, "--json")
    assert rc == 1 and out["code"] == "schema"
