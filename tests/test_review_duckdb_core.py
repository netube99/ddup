"""核心持久化链路（DuckDB）审查回归测试。

锁定审查修复的三个 P1：
1. 账本衍生表 run_id 固定为 LIVE_RUN_ID=1，且不被 sequence 空洞错位；
2. sync.yaml 的 statement 级 date 作为 fills 缺省日期；
3. 旧格式（SQLite）文件在 sweep 入口即拒止，不静默走 sqlite_scanner。
"""

import argparse
import sqlite3
import sys

import pytest
import yaml

from btcore import database
from btcore.provider import DataProvider
from research.live import LIVE_RUN_ID, LedgerStore
from tests.conftest import MockDataBackend, make_account, make_holding

NOW = "2026-09-18T00:00:00"


class _ClosableMockBackend(MockDataBackend):
    def close(self):
        pass


def _make_provider():
    return DataProvider(_ClosableMockBackend())


def _daily_row(date="20240603", cash=1_000_000.0, total_value=1_000_000.0):
    return {
        "date": date, "cash": cash, "total_value": total_value,
        "daily_pnl": 0.0, "cumulative_pnl": 0.0, "n_holdings": 1,
    }


def _account_with_holding():
    return make_account(
        cash=989_998.0, initial_capital=1_000_000.0,
        holdings={"000001.SZ": make_holding(
            symbol="000001.SZ", shares=1000, entry_date="20240603",
            entry_price=10.0,
        )},
    )


class TestLedgerDerivedRunId:
    """LIVE_RUN_ID=1 契约：runs 行必须与衍生数据同 run_id。"""

    def _store(self, tmp_path):
        store = LedgerStore(str(tmp_path / "ledger.duckdb"))
        store.init_account("20240603", 1_000_000.0, 1_000_000.0)
        store.append_fill("20240603", "000001.SZ", "BUY", price=10.0,
                          shares=1000, commission=2.0, created_at=NOW)
        store.conn.commit()
        return store

    def test_rewrite_derived_pins_live_run_id_after_sequence_gap(self, tmp_path):
        """sequence 空洞（事务回滚/崩溃）后，runs 行仍须是 run_id=1。"""
        store = self._store(tmp_path)
        try:
            # 模拟首次 rewrite_derived 事务回滚：sequence 已消耗，runs 行不存在
            store.conn.execute("SELECT nextval('runs_run_id_seq')")
            store.rewrite_derived([_daily_row()], [], _account_with_holding(),
                                  1_000_000.0)
            runs = store.conn.execute(
                "SELECT run_id, strategy FROM runs"
            ).fetchall()
            assert runs == [(LIVE_RUN_ID, "live")]
            assert store.conn.execute(
                "SELECT DISTINCT run_id FROM account_daily"
            ).fetchall() == [(LIVE_RUN_ID,)]
        finally:
            store.close()

    def test_rewrite_derived_does_not_accumulate_runs_rows(self, tmp_path):
        """重复 signal 不追加孤儿 runs 行。"""
        store = self._store(tmp_path)
        try:
            account = _account_with_holding()
            store.rewrite_derived([_daily_row()], [], account, 1_000_000.0)
            store.rewrite_derived([_daily_row()], [], account, 1_000_000.0)
            assert store.conn.execute(
                "SELECT COUNT(*) FROM runs"
            ).fetchone()[0] == 1
        finally:
            store.close()

    def test_rewrite_derived_refuses_foreign_run_row(self, tmp_path):
        """run_id=1 已被回测占用时 fail-fast，不得静默覆写其数据。"""
        store = self._store(tmp_path)
        try:
            database.write_run(
                store.conn, run_id=LIVE_RUN_ID, created_at=NOW,
                strategy="SomeBacktest", start_date="20240603",
                end_date="20240607", initial_capital=1_000_000.0,
                config_json="{}", status="completed",
            )
            store.conn.execute(
                "INSERT INTO account_daily (run_id, date, cash, total_value,"
                " initial_capital, n_holdings) VALUES (1, '20240603', 1, 1, 1, 0)"
            )
            store.conn.commit()
            with pytest.raises(ValueError, match="非账本 run"):
                store.rewrite_derived([_daily_row()], [], _account_with_holding(),
                                      1_000_000.0)
            assert store.conn.execute(
                "SELECT COUNT(*) FROM account_daily WHERE run_id = 1"
            ).fetchone()[0] == 1
        finally:
            store.close()


class TestSyncFillDateDefault:
    """sync.yaml 的 statement 级 date 是所有 fills 的缺省成交日。"""

    def _ledger(self, tmp_path):
        db = str(tmp_path / "ledger.duckdb")
        store = LedgerStore(db)
        store.init_account("20240603", 1_000_000.0, 1_000_000.0)
        store.append_fill("20240603", "000001.SZ", "BUY", price=10.0,
                          shares=1000, commission=2.0, created_at=NOW)
        store.conn.commit()
        store.close()
        return db

    def _sync_file(self, tmp_path, fills):
        path = tmp_path / "sync.yaml"
        path.write_text(yaml.dump({
            "date": "20240604", "cash": 991_097.0,
            "holdings": [{"symbol": "000001.SZ", "shares": 900}],
            "fills": fills,
        }))
        return str(path)

    def test_append_fills_idempotent_default_date(self, tmp_path):
        store = LedgerStore(str(tmp_path / "ledger.duckdb"))
        try:
            store.init_account("20240603", 1_000_000.0, 1_000_000.0)
            store.append_fills_idempotent(
                [{"symbol": "000001.SZ", "side": "SELL", "price": 11.0,
                  "shares": 100}],
                NOW, default_date="20240604",
            )
            assert store.fills()[-1]["date"] == "20240604"
        finally:
            store.close()

    def test_cmd_sync_applies_fill_without_date(self, tmp_path, monkeypatch):
        """文档格式（fills 不带 date）必须正常对账通过并落库。"""
        import scripts.live as live_cli

        db = self._ledger(tmp_path)
        sync_file = self._sync_file(tmp_path, [{
            "symbol": "000001.SZ", "side": "SELL", "price": 11.0,
            "shares": 100, "commission": 1.0,
        }])
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _make_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(db=db, file=sync_file))
        assert rc == 0
        store = LedgerStore(db)
        try:
            last = store.fills()[-1]
            assert (last["date"], last["side"], last["shares"]) == (
                "20240604", "SELL", 100)
        finally:
            store.close()

    def test_cmd_sync_reconcile_mismatch_rolls_back_fill(self, tmp_path, monkeypatch):
        """对账不一致 → 本次 fills 全部回滚（不落库）。"""
        import scripts.live as live_cli

        db = self._ledger(tmp_path)
        # 错报持仓：券商实际 950 ≠ 衍生 900 → 拒绝
        path = tmp_path / "bad_sync.yaml"
        path.write_text(yaml.dump({
            "date": "20240604", "cash": 991_097.0,
            "holdings": [{"symbol": "000001.SZ", "shares": 950}],
            "fills": [{"symbol": "000001.SZ", "side": "SELL", "price": 11.0,
                       "shares": 100, "commission": 1.0}],
        }))
        monkeypatch.setattr(live_cli.cli_common, "make_provider", _make_provider)
        rc = live_cli.cmd_sync(argparse.Namespace(db=db, file=str(path)))
        assert rc == 1
        store = LedgerStore(db)
        try:
            assert [f["side"] for f in store.fills()] == ["BUY"]
        finally:
            store.close()


class TestSweepLegacyFormat:
    """sweep 输出库同样走 assert_duckdb_file 拒止。"""

    def test_sweep_rejects_sqlite_out_before_subprocess(self, tmp_path, monkeypatch):
        import scripts.sweep as sweep_mod

        out = tmp_path / "old.db"
        con = sqlite3.connect(str(out))
        con.execute("CREATE TABLE runs (run_id INTEGER, stats_json TEXT)")
        con.execute("INSERT INTO runs VALUES (1, '{}')")
        con.commit()
        con.close()

        base = tmp_path / "base.yaml"
        base.write_text("top_k: 5\n")
        cfg = tmp_path / "sweep.yaml"
        cfg.write_text(yaml.dump({"base": str(base), "params": {"top_k": [5]}}))

        calls = []
        monkeypatch.setattr(
            sweep_mod.subprocess, "run",
            lambda *a, **k: calls.append(a),
        )
        monkeypatch.setattr(sys, "argv", [
            "sweep.py", str(cfg), "--start", "20240101", "--end", "20240131",
            "--out", str(out),
        ])
        with pytest.raises(ValueError, match="不是 DuckDB 格式"):
            sweep_mod.main()
        assert calls == []


class TestInitAtomicity:
    """建账原子性（F-CORE-07）：元数据 + OPENING 持仓同一事务，失败不留半账本。"""

    def test_init_account_does_not_commit_inside_transaction(self, tmp_path):
        store = LedgerStore(str(tmp_path / "ledger.duckdb"))
        try:
            store.conn.begin()
            store.init_account("20240603", 1_000_000.0, 1_000_000.0)
            store.conn.rollback()
            assert store.is_initialized() is False
        finally:
            store.close()

    def test_cmd_init_bad_position_leaves_ledger_uninitialized(self, tmp_path):
        import scripts.live as live_cli

        db = str(tmp_path / "ledger.duckdb")
        pos = tmp_path / "positions.yaml"
        pos.write_text(yaml.dump({"positions": [
            {"symbol": "000001.SZ", "entry_date": "20240603",
             "entry_price": 10.0, "shares": "not-an-int"},
        ]}))
        rc = live_cli.cmd_init(argparse.Namespace(
            db=db, date="20240603", cash=1_000_000.0, positions=str(pos),
        ))
        assert rc == 2
        store = LedgerStore(db)
        try:
            assert store.is_initialized() is False
            assert store.fills() == []
        finally:
            store.close()

    def test_cmd_init_valid_positions_commits_metadata_and_fills(self, tmp_path):
        import scripts.live as live_cli

        db = str(tmp_path / "ledger.duckdb")
        pos = tmp_path / "positions.yaml"
        pos.write_text(yaml.dump({"positions": [
            {"symbol": "000001.SZ", "entry_date": "20240603",
             "entry_price": 10.0, "shares": 1000},
        ]}))
        rc = live_cli.cmd_init(argparse.Namespace(
            db=db, date="20240603", cash=1_000_000.0, positions=str(pos),
        ))
        assert rc == 0
        store = LedgerStore(db)
        try:
            assert store.is_initialized() is True
            assert [(f["side"], f["shares"]) for f in store.fills()] == [
                ("OPENING", 1000)]
        finally:
            store.close()


class TestLedgerRunsEndDate:
    """F-CORE-09：signal 推进后 runs.end_date 跟随回放末日。"""

    def test_rewrite_derived_updates_end_date(self, tmp_path):
        store = LedgerStore(str(tmp_path / "ledger.duckdb"))
        try:
            store.init_account("20240603", 1_000_000.0, 1_000_000.0)
            account = _account_with_holding()
            store.rewrite_derived([_daily_row("20240603")], [], account,
                                  1_000_000.0)
            store.rewrite_derived([_daily_row("20240610")], [], account,
                                  1_000_000.0)
            assert store.conn.execute(
                "SELECT end_date FROM runs WHERE run_id = ?", [LIVE_RUN_ID]
            ).fetchone()[0] == "20240610"
        finally:
            store.close()
