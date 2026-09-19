"""实盘账本（research.live）测试：成交应用 / 对账 / 操作单 / 回测往返一致性。

核心不变式：把回测 trade_log 当账本灌进回放，衍生账户轨迹必须与回测
逐日逐分钱一致，末日 pending_actions 逐键相等——证明实盘路径 ≡ 回测路径。
"""

import argparse
import json
import sys
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

import scripts.live as live_cli
from btcore import database, types
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from research.live import (
    LEDGER_SCHEMA_VERSION,
    LedgerStore,
    apply_fill,
    build_op_sheet,
    reconcile,
    run_signal,
    seed_opening,
    validate_fill_dates,
)
from tests.conftest import MockDataBackend, make_account

EXAMPLE_YAML = "strategies/examples/rolling_ranker/config.yaml"
START, END = "20240603", "20240628"
NOW = "2026-08-03T00:00:00"


def _fill(date, symbol, side, price=10.0, shares=100, commission=0.0,
          stamp_tax=0.0, transfer_fee=0.0, reason="MANUAL"):
    return {"date": date, "symbol": symbol, "side": side, "price": price,
            "shares": shares, "commission": commission, "stamp_tax": stamp_tax,
            "transfer_fee": transfer_fee, "reason": reason}


def _run_backtest(tmp_path):
    """跑一次 rolling_ranker 回测（TestBacktestParity / TestOpSheet 共用）。"""
    strategy = load_strategy(EXAMPLE_YAML)
    provider = DataProvider(MockDataBackend())
    engine = Engine(strategy, provider, initial_capital=1_000_000,
                    db_path=str(tmp_path / "bt.duckdb"))
    engine.run(START, END)
    return engine


def _ledger_from_backtest(bt_engine, tmp_path):
    """把回测 trade_log 灌进 LedgerStore（BUY/SELL 行按 id 顺序）。"""
    store = LedgerStore(str(tmp_path / "ledger.duckdb"))
    store.init_account(START, 1_000_000.0, 1_000_000.0)
    conn = database.connect_result_db(str(tmp_path / "bt.duckdb"), read_only=True)
    rows = conn.execute(
        "SELECT date, symbol, side, trigger, price, shares, commission,"
        " stamp_tax, transfer_fee FROM trade_log"
        " WHERE side IN ('BUY','SELL') ORDER BY id"
    ).fetchall()
    conn.close()
    for date, symbol, side, trig, price, shares, comm, tax, fee in rows:
        store.append_fill(date, symbol, side, price=price, shares=shares,
                          commission=comm, stamp_tax=tax, transfer_fee=fee,
                          reason=trig, created_at=NOW)
    store.conn.commit()
    return store


class TestApplyFill:
    def test_buy_new_creates_locked_holding(self):
        acc = make_account(cash=100_000)
        trade = apply_fill(acc, _fill("20240603", "A", "BUY", price=10.0,
                                      shares=1000, commission=2.0), "20240603")
        h = acc.holdings["A"]
        assert h.shares == 1000 and h.locked is True
        assert h.entry_date == "20240603" and h.entry_price == 10.0
        assert acc.cash == pytest.approx(100_000 - 10_000 - 2.0)
        assert trade.net_amount == pytest.approx(-10_002.0)

    def test_buy_add_weighted_average_keeps_entry_date(self):
        acc = make_account(cash=100_000)
        apply_fill(acc, _fill("20240603", "A", "BUY", price=10.0, shares=1000), "20240603")
        apply_fill(acc, _fill("20240604", "A", "BUY", price=20.0, shares=1000), "20240604")
        h = acc.holdings["A"]
        assert h.shares == 2000
        assert h.entry_price == pytest.approx(15.0)
        assert h.entry_date == "20240603"  # 保留原买入日（红利税持股期口径）

    def test_sell_partial_and_full(self):
        acc = make_account(cash=100_000)
        apply_fill(acc, _fill("20240603", "A", "BUY", price=10.0, shares=1000), "20240603")
        acc.cash = 0  # 聚焦卖出现金流（持仓已在账）
        trade = apply_fill(acc, _fill("20240604", "A", "SELL", price=12.0,
                                      shares=400, stamp_tax=6.0), "20240604")
        assert acc.holdings["A"].shares == 600
        assert acc.cash == pytest.approx(4800 - 6.0)
        assert trade.net_amount == pytest.approx(4794.0)
        apply_fill(acc, _fill("20240605", "A", "SELL", price=12.0, shares=600), "20240605")
        assert "A" not in acc.holdings

    def test_sell_unheld_raises(self):
        acc = make_account(cash=0)
        with pytest.raises(ValueError, match="缺买入记录"):
            apply_fill(acc, _fill("20240603", "A", "SELL"), "20240603")

    def test_oversell_raises(self):
        acc = make_account(cash=100_000)
        apply_fill(acc, _fill("20240603", "A", "BUY", shares=100), "20240603")
        with pytest.raises(ValueError, match="账本不一致"):
            apply_fill(acc, _fill("20240604", "A", "SELL", shares=200), "20240604")

    def test_adjust_moves_cash_without_trade(self):
        acc = make_account(cash=1000)
        assert apply_fill(acc, _fill("20240603", "", "ADJUST", price=-50.0),
                          "20240603") is None
        assert acc.cash == pytest.approx(950.0)


class TestSeedOpening:
    def test_holding_days_seed_matches_continuous_run(self):
        """种子 holding_days + 回放递增 = 引擎从买入日起连续运行的口径。"""
        provider = DataProvider(MockDataBackend())
        init = "20240605"
        store = LedgerStore(":memory:")
        store.init_account(init, 50_000.0, 60_000.0)
        # entry 20240603 买入 → 0603/0604 两晚结算 → init 日递增前种子=2
        store.append_fill("20240603", "000001.SZ", "OPENING", price=10.0,
                          shares=100, reason="OPENING", created_at=NOW)
        acc = types.Account(cash=50_000.0, initial_capital=60_000.0)
        seed_opening(acc, store, provider, init)
        h = acc.holdings["000001.SZ"]
        assert h.holding_days == 2  # 种子 = entry 至 init 前的结算次数
        assert h.locked is False    # entry < init
        h.holding_days += 1         # 模拟 init 日 compute_pending 递增
        assert h.holding_days == 3  # 与 0603 买入、0605 晚结算的引擎口径一致

    def test_locked_when_entry_is_init_date(self):
        provider = DataProvider(MockDataBackend())
        store = LedgerStore(":memory:")
        store.init_account(START, 50_000.0, 60_000.0)
        store.append_fill(START, "000001.SZ", "OPENING", price=10.0,
                          shares=100, reason="OPENING", created_at=NOW)
        acc = types.Account(cash=50_000.0, initial_capital=60_000.0)
        seed_opening(acc, store, provider, START)
        assert acc.holdings["000001.SZ"].locked is True


class TestReconcile:
    def _store_with_fills(self):
        store = LedgerStore(":memory:")
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        store.append_fill(START, "000001.SZ", "BUY", price=10.0,
                          shares=1000, commission=2.0, created_at=NOW)
        store.conn.commit()
        return store

    def test_match(self):
        store = self._store_with_fills()
        provider = DataProvider(MockDataBackend())
        report = reconcile(store, provider, "20240604", 989_998.0,
                           {"000001.SZ": 1000})
        assert report.ok and report.holding_diffs == {}
        assert report.cash_delta == pytest.approx(0.0)

    def test_holding_mismatch_detected(self):
        store = self._store_with_fills()
        provider = DataProvider(MockDataBackend())
        report = reconcile(store, provider, "20240604", 989_998.0,
                           {"000001.SZ": 900})
        assert not report.ok
        assert report.holding_diffs == {"000001.SZ": (1000, 900)}

    def test_cash_delta_reported(self):
        store = self._store_with_fills()
        provider = DataProvider(MockDataBackend())
        report = reconcile(store, provider, "20240604", 989_000.0,
                           {"000001.SZ": 1000})
        assert report.ok
        assert report.cash_delta == pytest.approx(-998.0)

    def test_non_trading_date_fill_not_replayed(self):
        """账本回放只遍历开市日：非开市 fill 是死行（CLI sync 已归一化日期）。

        回归护栏：提示任何直接写账本的调用方必须落到交易日，否则对账永远不收敛。
        """
        store = LedgerStore(":memory:")
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        store.append_fill("20240608", "000001.SZ", "BUY", price=10.0,
                          shares=100, created_at=NOW)  # 周六
        store.conn.commit()
        provider = DataProvider(MockDataBackend())
        report = reconcile(store, provider, "20240611", 1_000_000.0, {})
        assert report.holding_diffs == {}


class TestSyncIdempotency:
    """sync 幂等：重复 statement 不双重入账（agent 重跑安全）。"""

    def test_duplicate_fills_skipped(self):
        store = LedgerStore(":memory:")
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        fills = [_fill(START, "000001.SZ", "BUY", price=10.0, shares=1000,
                       commission=2.0)]
        appended, skipped = store.append_fills_idempotent(fills, NOW)
        assert (appended, skipped) == (1, 0)
        appended, skipped = store.append_fills_idempotent(fills, NOW)
        assert (appended, skipped) == (0, 1)  # 完全重复 → 跳过
        # 同交易不同费用 → 视为不同（reconcile 会暴露差额），不静默吞
        fills[0]["commission"] = 2.5
        appended, skipped = store.append_fills_idempotent(fills, NOW)
        assert (appended, skipped) == (1, 0)
        assert len(store.fills()) == 2

    def test_lowercase_side_is_idempotent(self):
        """side 大小写归一化后判重（入库统一 upper），重跑不得双写。"""
        store = LedgerStore(":memory:")
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        fills = [_fill(START, "000001.SZ", "buy", price=10.0, shares=1000,
                       commission=2.0)]
        assert store.append_fills_idempotent(fills, NOW) == (1, 0)
        assert store.append_fills_idempotent(fills, NOW) == (0, 1)
        assert [f["side"] for f in store.fills()] == ["BUY"]

    def test_duplicate_fill_within_one_batch_skipped(self):
        """同一 statement 内完全重复的两条 fill 只入一条。"""
        store = LedgerStore(":memory:")
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        f = _fill(START, "000001.SZ", "buy", price=10.0, shares=1000)
        assert store.append_fills_idempotent([f, dict(f)], NOW) == (1, 1)
        assert len(store.fills()) == 1

    def test_bad_data_reports_error_json_not_crash(self):
        """缺买入记录的卖出 → data_error 而非堆栈崩溃（CLI 层由 cmd_sync 兜底，
        此处验证 append + reconcile 路径抛 ValueError）。"""
        store = LedgerStore(":memory:")
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        store.append_fills_idempotent(
            [_fill(START, "000001.SZ", "SELL", price=10.0, shares=100)],
            NOW,
        )
        provider = DataProvider(MockDataBackend())
        with pytest.raises(ValueError, match="缺买入记录"):
            reconcile(store, provider, START, 990_000.0, {})


class TestBacktestParity:
    """验收测试：回测 trade_log → 账本 → 回放 ≡ 回测。"""

    def test_round_trip_parity(self, tmp_path):
        bt = _run_backtest(tmp_path)
        assert bt.run_id > 0
        n_trades = database.connect_result_db(
            str(tmp_path / "bt.duckdb"), read_only=True
        ).execute("SELECT 1 FROM trade_log").fetchall()
        assert len(n_trades) > 0, "回测无成交，parity 测试失去意义"

        store = _ledger_from_backtest(bt, tmp_path)
        strategy = load_strategy(EXAMPLE_YAML)
        provider = DataProvider(MockDataBackend())
        live_engine, calendar = run_signal(strategy, provider, store, END)
        assert calendar[-1] == END

        # 1) 期末账户逐分钱一致
        la, ba = live_engine.account, bt.account
        assert la.cash == pytest.approx(ba.cash, abs=1e-6)
        assert la.total_value == pytest.approx(ba.total_value, abs=1e-6)
        assert set(la.holdings) == set(ba.holdings)
        for symbol in ba.holdings:
            lh, bh = la.holdings[symbol], ba.holdings[symbol]
            assert lh.shares == bh.shares
            assert lh.entry_price == pytest.approx(bh.entry_price, abs=1e-9)
            assert lh.holding_days == bh.holding_days

        # 2) 末日 pending_actions 逐键相等（明日操作单与回测决策一致）
        assert live_engine.pending_actions == bt.pending_actions

        # 3) 衍生 account_daily 与回测逐日一致
        bt_conn = database.connect_result_db(
            str(tmp_path / "bt.duckdb"), read_only=True
        )
        bt_daily = bt_conn.execute(
            "SELECT date, cash, total_value FROM account_daily ORDER BY date"
        ).fetchall()
        bt_conn.close()
        live_daily = store.conn.execute(
            "SELECT date, cash, total_value FROM account_daily"
            " WHERE run_id = 1 ORDER BY date"
        ).fetchall()
        assert len(live_daily) == len(bt_daily)
        for (ld, lc, lv), (bd, bc, bv) in zip(live_daily, bt_daily):
            assert ld == bd
            assert lc == pytest.approx(bc, abs=1e-6)
            assert lv == pytest.approx(bv, abs=1e-6)

        # 4) 幂等：再跑一次 signal，衍生表不变
        strategy2 = load_strategy(EXAMPLE_YAML)
        provider2 = DataProvider(MockDataBackend())
        run_signal(strategy2, provider2, store, END)
        live_daily2 = store.conn.execute(
            "SELECT date, cash, total_value FROM account_daily"
            " WHERE run_id = 1 ORDER BY date"
        ).fetchall()
        assert live_daily2 == live_daily


class TestOpSheet:
    def test_op_sheet_structure_and_reasons(self, tmp_path):
        bt = _run_backtest(tmp_path)
        store = _ledger_from_backtest(bt, tmp_path)
        strategy = load_strategy(EXAMPLE_YAML)
        provider = DataProvider(MockDataBackend())
        engine, calendar = run_signal(strategy, provider, store, END)
        sheet = build_op_sheet(engine, provider, calendar[-1])

        assert sheet["signal_date"] == END
        assert sheet["trade_date"] and sheet["trade_date"] > END
        acct = sheet["account"]
        assert acct["cash"] == pytest.approx(engine.account.cash, abs=0.01)
        # 卖单带 reason（缺省 MANUAL），买单带预估股数口径
        for s in sheet["open_sells"]:
            assert s["reason"] and s["shares"]
        for b in sheet["open_buys"]:
            assert "est_shares" in b and "ref_close" in b
        # 条件单监控表覆盖所有带条件单的持仓
        cond_syms = {c["symbol"] for c in sheet["broker_conditions"]}
        expected = {s for s, h in engine.account.holdings.items() if h.conditions}
        assert cond_syms == expected


class TestSellReasonsProtocol:
    """select 协议 sell_reasons 键（TREND_BREAK 归因透传）。"""

    def _strategy(self, reasons):
        from btcore.strategy import Strategy

        class _R(Strategy):
            def select(self, bars, snapshot, provider):
                sells = list(snapshot.holdings.keys())
                if not sells and not snapshot.holdings:
                    # 首日建仓一只，次日卖出带 reason
                    return {"buy": ["000001.SZ"], "sell": []}
                return {"buy": [], "sell": sells,
                        "sell_reasons": {s: reasons for s in sells}}

            def calc_conditions(self, symbol, entry_price, bar, holding_days):
                return []

        return _R(config={"max_positions": 5, "initial_capital": 1_000_000})

    def test_reason_becomes_trade_trigger(self, tmp_path):
        engine = Engine(self._strategy("TREND_BREAK"),
                        DataProvider(MockDataBackend()),
                        initial_capital=1_000_000, db_path=str(tmp_path / "r.duckdb"))
        engine.run("20240603", "20240607")
        conn = database.connect_result_db(str(tmp_path / "r.duckdb"), read_only=True)
        triggers = [r[0] for r in conn.execute(
            "SELECT trigger FROM trade_log WHERE side = 'SELL'").fetchall()]
        conn.close()
        assert triggers and all(t == "TREND_BREAK" for t in triggers)

    def test_reason_symbol_must_be_in_sell(self):
        from btcore.strategy import Strategy

        class _Bad(Strategy):
            def select(self, bars, snapshot, provider):
                return {"buy": [], "sell": [],
                        "sell_reasons": {"000001.SZ": "X"}}

            def calc_conditions(self, symbol, entry_price, bar, holding_days):
                return []

        engine = Engine(_Bad(config={"max_positions": 5,
                                     "initial_capital": 1_000_000}),
                        DataProvider(MockDataBackend()), db_path=":memory:")
        with pytest.raises(ValueError, match="不在 sell 名单"):
            engine.run("20240603", "20240607")


# ── 审计修复回归（LIVE-01..15 / INFRA-05）──


class _ClosableMockBackend(MockDataBackend):
    """MockDataBackend + close()：LIVE CLI 会关闭 provider.backend。"""

    def close(self):
        pass


class _ExtraCalendarBackend(_ClosableMockBackend):
    """trade_cal 含 20240702（开市）但无 bars——模拟日历先行、数据未更新。"""

    def __init__(self):
        super().__init__()
        extra = pd.DataFrame([{
            "cal_date": "20240702", "is_open": 1, "exchange": "SSE",
        }])
        self._trade_cal = pd.concat([self._trade_cal, extra], ignore_index=True)


def _mock_provider() -> DataProvider:
    return DataProvider(_ClosableMockBackend())


def _extra_cal_provider() -> DataProvider:
    return DataProvider(_ExtraCalendarBackend())


def _json_out(capsys) -> dict:
    out = capsys.readouterr().out
    return json.loads(out[out.index("{"):out.rindex("}") + 1])


def _ledger_with_buy(tmp_path, name="ledger.duckdb", date=START,
                     cash=1_000_000.0, symbol="000001.SZ",
                     price=10.0, shares=1000) -> LedgerStore:
    store = LedgerStore(str(tmp_path / name))
    store.init_account(date, cash, cash)
    store.append_fill(date, symbol, "BUY", price=price, shares=shares,
                      commission=2.0, created_at=NOW)
    store.conn.commit()
    return store


def _sync_yaml(tmp_path, name, *, date, cash, holdings=None, fills=None) -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump({
        "date": date, "cash": cash,
        "holdings": holdings if holdings is not None else [],
        "fills": fills or [],
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return str(path)


class TestLedgerSchemaVersion:
    """LIVE-14：init 写 schema_version；未知版本 fail-fast，缺失兼容旧账本。"""

    def test_init_writes_schema_version(self):
        store = LedgerStore(":memory:")
        try:
            store.init_account(START, 1_000_000.0, 1_000_000.0)
            assert store.get_meta("schema_version") == str(LEDGER_SCHEMA_VERSION)
        finally:
            store.close()

    def test_unknown_version_rejected(self, tmp_path):
        db = str(tmp_path / "ledger.duckdb")
        store = LedgerStore(db)
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        store.set_meta("schema_version", "99")
        store.conn.commit()
        store.close()
        with pytest.raises(ValueError, match="schema_version"):
            LedgerStore(db)

    def test_missing_version_is_legacy_compatible(self, tmp_path):
        db = str(tmp_path / "ledger.duckdb")
        store = LedgerStore(db)
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        store.conn.execute("DELETE FROM ledger_meta WHERE key = 'schema_version'")
        store.conn.commit()
        store.close()
        reopened = LedgerStore(db)
        try:
            assert reopened.is_initialized()
        finally:
            reopened.close()


class TestNumericGuards:
    """LIVE-02：NaN/Inf 在 init/append_fill/apply_fill/reconcile 全部拒绝。"""

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
    def test_init_account_rejects_nonfinite(self, bad):
        store = LedgerStore(":memory:")
        try:
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                store.init_account(START, bad, 1_000_000.0)
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                store.init_account(START, 1_000_000.0, bad)
            assert not store.is_initialized()
        finally:
            store.close()

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_append_fill_rejects_nonfinite(self, bad):
        store = LedgerStore(":memory:")
        try:
            store.init_account(START, 1_000_000.0, 1_000_000.0)
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                store.append_fill(START, "000001.SZ", "BUY", price=bad,
                                  shares=100, created_at=NOW)
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                store.append_fill(START, "000001.SZ", "BUY", price=10.0,
                                  shares=100, commission=bad, created_at=NOW)
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                store.append_fill(START, "", "ADJUST", price=bad, created_at=NOW)
            assert store.fills() == []
        finally:
            store.close()

    def test_apply_fill_rejects_nonfinite(self):
        acc = make_account(cash=100_000)
        with pytest.raises(ValueError, match="NaN/Inf|非法"):
            apply_fill(acc, _fill(START, "A", "BUY", price=float("nan")), START)
        with pytest.raises(ValueError, match="NaN/Inf|非法"):
            apply_fill(acc, _fill(START, "A", "BUY", commission=float("inf")), START)

    def test_reconcile_rejects_nonfinite_cash(self):
        store = LedgerStore(":memory:")
        try:
            store.init_account(START, 1_000_000.0, 1_000_000.0)
            provider = _mock_provider()
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                reconcile(store, provider, START, float("nan"), {})
        finally:
            store.close()

    def test_legacy_nan_ledger_meta_fails_fast(self, tmp_path):
        db = str(tmp_path / "ledger.duckdb")
        store = LedgerStore(db)
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        store.set_meta("initial_cash", "nan")
        store.conn.commit()
        store.close()
        store = LedgerStore(db)
        try:
            with pytest.raises(ValueError, match="NaN/Inf|非法"):
                store.initial_cash
        finally:
            store.close()


class TestApplyFillNegativeCash:
    """LIVE-13：应用 fill 后现金为负 → fail-fast，不允许持久化负现金。"""

    def test_buy_insufficient_cash_raises(self):
        acc = make_account(cash=500.0)
        with pytest.raises(ValueError, match="现金为负"):
            apply_fill(acc, _fill(START, "A", "BUY", price=10.0, shares=100), START)

    def test_adjust_below_zero_raises(self):
        acc = make_account(cash=100.0)
        with pytest.raises(ValueError, match="现金为负"):
            apply_fill(acc, _fill(START, "", "ADJUST", price=-200.0), START)

    def test_cmd_sync_negative_statement_cash_rejected(self, tmp_path, monkeypatch,
                                                       capsys):
        store = _ledger_with_buy(tmp_path)
        store.close()
        path = _sync_yaml(tmp_path, "neg.yaml", date=START, cash=-5000.0,
                          holdings=[{"symbol": "000001.SZ", "shares": 1000}])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(
            db=str(tmp_path / "ledger.duckdb"), file=path))
        assert rc == 2
        out = _json_out(capsys)
        assert out["ok"] is False and out["stage"] == "parse_error"
        assert "负" in out["message"] or "negative" in out["message"]


class TestIdempotencyPrecision:
    """LIVE-15：幂等归一化仅用于判重，落库保留原始数值。"""

    def test_original_price_and_fees_preserved(self):
        store = LedgerStore(":memory:")
        try:
            store.init_account(START, 1_000_000.0, 1_000_000.0)
            fills = [_fill(START, "000001.SZ", "BUY", price=11.12345678,
                           shares=1000, commission=2.12345678)]
            assert store.append_fills_idempotent(fills, NOW) == (1, 0)
            assert store.append_fills_idempotent(fills, NOW) == (0, 1)
            row = store.fills()[0]
            assert row["price"] == 11.12345678
            assert row["commission"] == 2.12345678
        finally:
            store.close()

    def test_fractional_shares_rejected(self):
        store = LedgerStore(":memory:")
        try:
            store.init_account(START, 1_000_000.0, 1_000_000.0)
            with pytest.raises(ValueError, match="整数"):
                store.append_fills_idempotent(
                    [_fill(START, "000001.SZ", "BUY", shares=1000.9)], NOW)
            assert store.fills() == []
        finally:
            store.close()


class TestSeedOpeningAnchor:
    """LIVE-06：init 非开市日时 seed 锚定 ≤init 的最近开市日（bisect 防御）。"""

    def test_init_on_non_trading_day_anchors_to_prev_open_day(self):
        provider = _mock_provider()
        store = LedgerStore(":memory:")
        try:
            store.init_account("20240608", 50_000.0, 60_000.0)  # 周六
            store.append_fill("20240603", "000001.SZ", "OPENING", price=10.0,
                              shares=100, reason="OPENING", created_at=NOW)
            acc = types.Account(cash=50_000.0, initial_capital=60_000.0)
            seed_opening(acc, store, provider, "20240608")
            h = acc.holdings["000001.SZ"]
            assert h.holding_days == 4  # 0603→0607 的结算次数
            assert h.locked is False
        finally:
            store.close()


class TestCmdInitDateValidation:
    """INFRA-05 / LIVE-06：init --date 格式 + 开市日校验。"""

    def _ns(self, db, date, cash=1_000_000.0, positions=None):
        return argparse.Namespace(db=db, date=date, cash=cash,
                                  positions=positions)

    def test_bad_format_rejected_without_creating_db(self, tmp_path, capsys):
        db = tmp_path / "l.duckdb"
        rc = live_cli.cmd_init(self._ns(str(db), "abc"), provider=_mock_provider())
        assert rc == 2
        out = _json_out(capsys)
        assert out["ok"] is False and out["stage"] == "bad_args"
        assert not db.exists()

    def test_bad_calendar_date_rejected(self, tmp_path, capsys):
        rc = live_cli.cmd_init(self._ns(str(tmp_path / "l.duckdb"), "20261301"),
                               provider=_mock_provider())
        assert rc == 2
        assert _json_out(capsys)["ok"] is False

    def test_non_trading_day_rejected_with_hint(self, tmp_path, capsys):
        rc = live_cli.cmd_init(self._ns(str(tmp_path / "l.duckdb"), "20240608"),
                               provider=_mock_provider())
        assert rc == 2
        out = _json_out(capsys)
        assert out["ok"] is False
        assert "20240607" in out["message"]

    def test_nan_cash_rejected(self, tmp_path, capsys):
        rc = live_cli.cmd_init(
            self._ns(str(tmp_path / "l.duckdb"), START, cash=float("nan")),
            provider=_mock_provider())
        assert rc == 2
        assert _json_out(capsys)["ok"] is False

    def test_valid_day_ok(self, tmp_path, capsys):
        rc = live_cli.cmd_init(self._ns(str(tmp_path / "l.duckdb"), START),
                               provider=_mock_provider())
        assert rc == 0
        out = _json_out(capsys)
        assert out["ok"] is True and out["start_date"] == START

    def test_nonfinite_position_rejected(self, tmp_path, capsys):
        positions = tmp_path / "p.yaml"
        positions.write_text(yaml.safe_dump({"positions": [
            {"symbol": "000001.SZ", "entry_date": START,
             "entry_price": float("nan"), "shares": 100},
        ]}))
        rc = live_cli.cmd_init(
            self._ns(str(tmp_path / "l.duckdb"), START,
                     positions=str(positions)), provider=_mock_provider())
        assert rc == 2
        assert _json_out(capsys)["ok"] is False

    def test_main_injects_provider_for_open_day_check(self, tmp_path, monkeypatch,
                                                      capsys):
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        monkeypatch.setattr(sys, "argv", [
            "live.py", "init", str(tmp_path / "l.duckdb"),
            "--date", "20240608", "--cash", "1000",
        ])
        assert live_cli.main() == 2
        out = _json_out(capsys)
        assert out["ok"] is False and "20240607" in out["message"]


class TestValidateFillDates:
    def test_flags_nontrading_and_uses_default_date(self):
        calendar = ["20240603", "20240604", "20240605"]
        fills = [
            _fill("20240603", "A", "BUY", shares=100),
            _fill("20240608", "B", "BUY", shares=100),
            _fill("", "000001.SZ", "BUY", shares=100),  # 缺省日 20240604
        ]
        problems = validate_fill_dates(fills, calendar, default_date="20240604")
        assert len(problems) == 1
        assert "第 2 行" in problems[0]
        assert "20240608" in problems[0] and "B" in problems[0]


class TestSyncFillDateValidation:
    """LIVE-01：非交易日/早于账本起始日的 fill 整体拒绝、不落库。"""

    def test_nontrading_fill_date_rejected_without_write(self, tmp_path, monkeypatch,
                                                         capsys):
        store = _ledger_with_buy(tmp_path)
        store.close()
        path = _sync_yaml(
            tmp_path, "s.yaml", date="20240608", cash=989_998.0,
            holdings=[{"symbol": "000001.SZ", "shares": 1000}],
            fills=[_fill("20240608", "000001.SZ", "BUY", price=10.0, shares=100)])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(
            db=str(tmp_path / "ledger.duckdb"), file=path))
        assert rc == 1
        out = _json_out(capsys)
        assert out["ok"] is False and out["stage"] == "invalid_fill_date"
        assert any("第 1 行" in p and "20240608" in p for p in out["problems"])
        store = LedgerStore(str(tmp_path / "ledger.duckdb"))
        try:
            assert [f["side"] for f in store.fills()] == ["BUY"]  # 无死行/ADJUST
        finally:
            store.close()

    def test_fill_before_start_date_rejected(self, tmp_path, monkeypatch, capsys):
        store = _ledger_with_buy(tmp_path)
        store.close()
        path = _sync_yaml(
            tmp_path, "s.yaml", date="20240604", cash=989_998.0,
            holdings=[{"symbol": "000001.SZ", "shares": 1000}],
            fills=[_fill("20240601", "000001.SZ", "BUY", price=10.0, shares=100)])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(
            db=str(tmp_path / "ledger.duckdb"), file=path))
        assert rc == 1
        assert _json_out(capsys)["stage"] == "invalid_fill_date"

    def test_valid_fill_date_accepted(self, tmp_path, monkeypatch, capsys):
        store = _ledger_with_buy(tmp_path)
        store.close()
        path = _sync_yaml(
            tmp_path, "s.yaml", date="20240604", cash=991_097.0,
            holdings=[{"symbol": "000001.SZ", "shares": 900}],
            fills=[_fill("20240604", "000001.SZ", "SELL", price=11.0, shares=100,
                         commission=1.0)])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(
            db=str(tmp_path / "ledger.duckdb"), file=path))
        assert rc == 0
        out = _json_out(capsys)
        assert out["ok"] is True and out["fills_applied"] == 1


class TestSyncFutureDate:
    """LIVE-07：statement date 晚于行情数据 → fail-fast（与 signal 口径一致）。"""

    def _prep(self, tmp_path):
        store = _ledger_with_buy(tmp_path)
        store.close()
        return str(tmp_path / "ledger.duckdb")

    def test_future_trading_day_without_data_rejected(self, tmp_path, monkeypatch,
                                                      capsys):
        db = self._prep(tmp_path)
        path = _sync_yaml(tmp_path, "f.yaml", date="20240702", cash=989_998.0,
                          holdings=[{"symbol": "000001.SZ", "shares": 1000}])
        monkeypatch.setattr(live_cli.cli_common, "make_provider",
                            _extra_cal_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(db=db, file=path))
        assert rc == 1
        out = _json_out(capsys)
        assert out["ok"] is False and out["stage"] == "no_market_data"
        store = LedgerStore(db)
        try:
            assert [f["side"] for f in store.fills()] == ["BUY"]  # 未落 ADJUST
        finally:
            store.close()

    def test_last_data_day_accepted(self, tmp_path, monkeypatch, capsys):
        db = self._prep(tmp_path)
        path = _sync_yaml(tmp_path, "ok.yaml", date="20240701", cash=989_998.0,
                          holdings=[{"symbol": "000001.SZ", "shares": 1000}])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(db=db, file=path))
        assert rc == 0
        assert _json_out(capsys)["ok"] is True


class TestSyncOpeningRejected:
    """LIVE-08：sync fills 拒绝 side: OPENING（仅 init positions 使用）。"""

    def test_opening_side_rejected(self, tmp_path, monkeypatch, capsys):
        store = _ledger_with_buy(tmp_path)
        store.close()
        path = _sync_yaml(
            tmp_path, "o.yaml", date="20240604", cash=989_998.0,
            holdings=[{"symbol": "000001.SZ", "shares": 1100}],
            fills=[_fill("20240604", "000002.SZ", "OPENING", price=10.0,
                         shares=100)])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(
            db=str(tmp_path / "ledger.duckdb"), file=path))
        assert rc == 1
        out = _json_out(capsys)
        assert out["ok"] is False and out["stage"] == "data_error"
        assert "OPENING" in out["message"]
        store = LedgerStore(str(tmp_path / "ledger.duckdb"))
        try:
            assert [f["side"] for f in store.fills()] == ["BUY"]
        finally:
            store.close()


class TestSyncStatementParsing:
    """LIVE-11：statement 解析错误进入 JSON 错误通道，不再裸异常。"""

    def _run(self, tmp_path, monkeypatch, capsys, *, statement=None, file=None):
        store = _ledger_with_buy(tmp_path)
        store.close()
        db = str(tmp_path / "ledger.duckdb")
        path = file
        if path is None and statement is not None:
            path = tmp_path / "s.yaml"
            path.write_text(yaml.safe_dump(statement, allow_unicode=True,
                                           sort_keys=False), encoding="utf-8")
            path = str(path)
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(db=db, file=path))
        return rc, _json_out(capsys)

    def test_cash_not_a_number(self, tmp_path, monkeypatch, capsys):
        rc, out = self._run(tmp_path, monkeypatch, capsys, statement={
            "date": "20240604", "cash": "abc",
            "holdings": [{"symbol": "000001.SZ", "shares": 1000}],
        })
        assert rc == 2
        assert out["ok"] is False and out["stage"] == "parse_error"
        assert "cash" in out["message"]

    def test_cash_nan(self, tmp_path, monkeypatch, capsys):
        rc, out = self._run(tmp_path, monkeypatch, capsys, statement={
            "date": "20240604", "cash": float("nan"),
            "holdings": [{"symbol": "000001.SZ", "shares": 1000}],
        })
        assert rc == 2
        assert out["ok"] is False and out["stage"] == "parse_error"

    def test_holdings_missing_shares(self, tmp_path, monkeypatch, capsys):
        rc, out = self._run(tmp_path, monkeypatch, capsys, statement={
            "date": "20240604", "cash": 989_998.0,
            "holdings": [{"symbol": "000001.SZ"}],
        })
        assert rc == 2
        assert out["ok"] is False and out["stage"] == "parse_error"
        assert "shares" in out["message"]

    def test_missing_file(self, tmp_path, monkeypatch, capsys):
        rc, out = self._run(tmp_path, monkeypatch, capsys,
                            file=str(tmp_path / "no_such.yaml"))
        assert rc == 2
        assert out["ok"] is False and out["stage"] == "parse_error"

    def test_fill_not_a_mapping(self, tmp_path, monkeypatch, capsys):
        rc, out = self._run(tmp_path, monkeypatch, capsys, statement={
            "date": "20240604", "cash": 989_998.0,
            "holdings": [{"symbol": "000001.SZ", "shares": 1000}],
            "fills": ["oops"],
        })
        assert rc == 2
        assert out["ok"] is False and out["stage"] == "parse_error"

    def test_fill_missing_side_is_data_error(self, tmp_path, monkeypatch, capsys):
        rc, out = self._run(tmp_path, monkeypatch, capsys, statement={
            "date": "20240604", "cash": 989_998.0,
            "holdings": [{"symbol": "000001.SZ", "shares": 1000}],
            "fills": [{"symbol": "000001.SZ", "price": 10.0, "shares": 100}],
        })
        assert rc == 1
        assert out["ok"] is False and out["stage"] == "data_error"


class TestCmdStatus:
    """LIVE-09：status 对不存在的库路径报错 exit 2，不静默建库。"""

    def test_missing_db_errors_without_creating(self, tmp_path, capsys):
        db = tmp_path / "nope.duckdb"
        rc = live_cli.cmd_status(argparse.Namespace(db=str(db)))
        assert rc == 2
        assert not db.exists()
        out = _json_out(capsys)
        assert out["ok"] is False

    def test_uninitialized_db_rejected(self, tmp_path, capsys):
        db = str(tmp_path / "empty.duckdb")
        LedgerStore(db).close()
        assert live_cli.cmd_status(argparse.Namespace(db=db)) == 2
        assert _json_out(capsys)["ok"] is False

    def test_non_ledger_duckdb_not_mutated(self, tmp_path, capsys):
        db = str(tmp_path / "bt.duckdb")
        conn = database.init_backtest_db(db)
        conn.execute("CREATE TABLE foo (x INTEGER)")
        conn.close()
        assert live_cli.cmd_status(argparse.Namespace(db=db)) == 2
        assert _json_out(capsys)["ok"] is False
        conn = database.connect_result_db(db, read_only=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT table_name FROM information_schema.tables").fetchall()}
        finally:
            conn.close()
        assert "foo" in tables and "ledger_meta" not in tables

    def test_initialized_db_ok(self, tmp_path, capsys):
        store = _ledger_with_buy(tmp_path)
        store.close()
        rc = live_cli.cmd_status(argparse.Namespace(
            db=str(tmp_path / "ledger.duckdb")))
        assert rc == 0
        assert _json_out(capsys)["start_date"] == START


class TestCmdSignalErrors:
    """LIVE-12：signal 业务错误打可读消息，不裸 traceback。"""

    def _ledger(self, tmp_path) -> str:
        store = _ledger_with_buy(tmp_path)
        store.close()
        return str(tmp_path / "ledger.duckdb")

    def _ns(self, db, yaml_path, date=None, out=None):
        return argparse.Namespace(db=db, yaml=yaml_path, date=date, out=out)

    def test_missing_yaml_readable(self, tmp_path, monkeypatch, capsys):
        db = self._ledger(tmp_path)
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_signal(self._ns(db, "no/such.yaml", date=START))
        assert rc == 2
        err = capsys.readouterr().err
        assert "YAML" in err and "Traceback" not in err

    def test_missing_out_dir_readable(self, tmp_path, monkeypatch, capsys):
        db = self._ledger(tmp_path)
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        out = str(tmp_path / "nodir" / "o.json")
        rc = live_cli.cmd_signal(self._ns(db, EXAMPLE_YAML, date=START, out=out))
        assert rc == 2
        err = capsys.readouterr().err
        assert "目录" in err and "Traceback" not in err

    def test_non_trading_date_readable(self, tmp_path, monkeypatch, capsys):
        db = self._ledger(tmp_path)
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _mock_provider)
        rc = live_cli.cmd_signal(self._ns(db, EXAMPLE_YAML, date="20240608"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "交易日" in err and "Traceback" not in err

    def test_no_market_data_readable(self, tmp_path, monkeypatch, capsys):
        db = self._ledger(tmp_path)
        monkeypatch.setattr(live_cli.cli_common, "make_provider",
                            _extra_cal_provider)
        rc = live_cli.cmd_signal(self._ns(db, EXAMPLE_YAML, date="20240702"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "无行情数据" in err and "Traceback" not in err


class TestOpSheetNotices:
    """LIVE-03/04：universe 外持仓用补价面板判停牌；t1_lock 死代码删除。"""

    def _engine(self, account):
        return SimpleNamespace(pending_actions={"buy": [], "sell": []},
                               account=account, max_positions=5,
                               bars_by_date={}, bars_df=None)

    def test_price_fallback_suppresses_false_suspended(self):
        provider = _mock_provider()
        acc = types.Account(cash=10_000.0, initial_capital=10_000.0)
        acc.holdings["000002.SZ"] = types.Holding(
            symbol="000002.SZ", shares=100, entry_date="20240603",
            entry_price=10.0, cost=1000.0, last_price=10.0, locked=True)
        acc.holdings["999999.XX"] = types.Holding(
            symbol="999999.XX", shares=100, entry_date="20240603",
            entry_price=10.0, cost=1000.0, last_price=10.0)
        sheet = build_op_sheet(self._engine(acc), provider, "20240628")
        suspended = {n["symbol"] for n in sheet["notices"]
                     if n["type"] == "suspended"}
        assert suspended == {"999999.XX"}
        assert all(n["type"] != "t1_lock" for n in sheet["notices"])
