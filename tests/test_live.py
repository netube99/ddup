"""实盘账本（research.live）测试：成交应用 / 对账 / 操作单 / 回测往返一致性。

核心不变式：把回测 trade_log 当账本灌进回放，衍生账户轨迹必须与回测
逐日逐分钱一致，末日 pending_actions 逐键相等——证明实盘路径 ≡ 回测路径。
"""

import sqlite3

import pytest

from btcore import types
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from research.live import (
    LedgerStore,
    apply_fill,
    build_op_sheet,
    reconcile,
    run_signal,
    seed_opening,
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
        acc = make_account(cash=0)
        apply_fill(acc, _fill("20240603", "A", "BUY", price=10.0, shares=1000), "20240603")
        acc.cash = 0  # 聚焦卖出现金流
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
        acc = make_account(cash=0)
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
        store.conn.rollback()

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
        store.conn.rollback()


class TestBacktestParity:
    """验收测试：回测 trade_log → 账本 → 回放 ≡ 回测。"""

    def _run_backtest(self, tmp_path):
        strategy = load_strategy(EXAMPLE_YAML)
        provider = DataProvider(MockDataBackend())
        engine = Engine(strategy, provider, initial_capital=1_000_000,
                        db_path=str(tmp_path / "bt.db"))
        engine.run(START, END)
        return engine

    def _ledger_from_backtest(self, bt_engine, tmp_path):
        store = LedgerStore(str(tmp_path / "ledger.db"))
        store.init_account(START, 1_000_000.0, 1_000_000.0)
        conn = sqlite3.connect(str(tmp_path / "bt.db"))
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

    def test_round_trip_parity(self, tmp_path):
        bt = self._run_backtest(tmp_path)
        assert bt.run_id > 0
        n_trades = len(
            sqlite3.connect(str(tmp_path / "bt.db")).execute(
                "SELECT 1 FROM trade_log").fetchall()
        )
        assert n_trades > 0, "回测无成交，parity 测试失去意义"

        store = self._ledger_from_backtest(bt, tmp_path)
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
        bt_daily = sqlite3.connect(str(tmp_path / "bt.db")).execute(
            "SELECT date, cash, total_value FROM account_daily ORDER BY date"
        ).fetchall()
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
        bt = TestBacktestParity()._run_backtest(tmp_path)
        store = TestBacktestParity()._ledger_from_backtest(bt, tmp_path)
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
                        initial_capital=1_000_000, db_path=str(tmp_path / "r.db"))
        engine.run("20240603", "20240607")
        conn = sqlite3.connect(str(tmp_path / "r.db"))
        triggers = [r[0] for r in conn.execute(
            "SELECT trigger FROM trade_log WHERE side = 'SELL'")]
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
