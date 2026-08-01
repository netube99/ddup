"""debug_snapshots 功能测试：debug 模式写入快照，非 debug 模式不写。"""

import json

from btcore.database import init_backtest_db, write_debug_snapshot
from btcore.engine import Engine
from btcore.provider import DataProvider
from tests.conftest import MockDataBackend
from tests.test_invariants.conftest import AccumulateBuyStrategy


class TestWriteDebugSnapshot:
    def test_writes_valid_json(self, tmp_path):
        """debug snapshot JSON 可写且可读。"""
        db_path = str(tmp_path / "test.db")
        conn = init_backtest_db(db_path)

        snap = {
            "date": "20240603",
            "account": {"cash": 900000.0, "total_value": 1000000.0, "n_holdings": 2},
            "pending": {"buy": ["000001.SZ"], "sell": [], "buy_conditions": []},
            "holdings_detail": {
                "600036.SH": {"shares": 10000, "entry_price": 35.0,
                              "entry_date": "20240601", "holding_days": 2},
            },
            "bars_subset": {"600036.SH": {"close": 36.0}},
        }
        write_debug_snapshot(conn, 1, "20240603", snap)
        conn.commit()

        row = conn.execute(
            "SELECT snapshot_json FROM debug_snapshots WHERE run_id=1 AND date='20240603'"
        ).fetchone()
        assert row is not None
        loaded = json.loads(row[0])
        assert loaded["date"] == "20240603"
        assert loaded["account"]["n_holdings"] == 2
        assert loaded["holdings_detail"]["600036.SH"]["shares"] == 10000
        conn.close()

    def test_insert_or_replace(self, tmp_path):
        """INSERT OR REPLACE 覆盖同 run_id+date。"""
        db_path = str(tmp_path / "test.db")
        conn = init_backtest_db(db_path)

        write_debug_snapshot(conn, 1, "20240603", {"date": "20240603", "v": 1})
        write_debug_snapshot(conn, 1, "20240603", {"date": "20240603", "v": 2})
        conn.commit()

        row = conn.execute(
            "SELECT snapshot_json FROM debug_snapshots WHERE run_id=1 AND date='20240603'"
        ).fetchone()
        loaded = json.loads(row[0])
        assert loaded["v"] == 2
        conn.close()


class TestEngineDebugMode:
    """Engine debug 模式集成测试：手动步进引擎验证快照行为。"""

    def _make_engine(self, debug):
        provider = DataProvider(MockDataBackend())
        bars_df = provider.get_engine_bars(None, "20240701")
        bars_df.sort_index(inplace=True)
        calendar = provider.get_calendar("20240603", "20240607")
        strategy = AccumulateBuyStrategy({"slippage_ticks": 0, "max_positions": 2})
        engine = Engine(strategy, provider, initial_capital=1_000_000,
                        db_path=":memory:", max_positions=2, debug=debug)
        engine.bars_df = bars_df
        engine.bars_by_date = {
            d: group.droplevel("trade_date")
            for d, group in bars_df.groupby(level="trade_date", sort=False)
        }
        strategy.on_start(provider, calendar[0])
        return engine, calendar

    def test_debug_writes_snapshots(self):
        """debug=True 时快照写入 DB。"""
        engine, calendar = self._make_engine(debug=True)
        conn = init_backtest_db(":memory:")
        try:
            engine.run_id = 1
            engine._compute_pending(calendar[0])
            for today in calendar:
                if today not in engine.bars_by_date:
                    continue
                engine.step(today, engine.bars_by_date[today], conn)

            rows = conn.execute(
                "SELECT COUNT(*) FROM debug_snapshots WHERE run_id = 1"
            ).fetchone()
            assert rows[0] > 0, "debug mode should write snapshots"
        finally:
            conn.close()

    def test_non_debug_no_snapshots(self):
        """debug=False 默认不写快照。"""
        engine, calendar = self._make_engine(debug=False)
        conn = init_backtest_db(":memory:")
        try:
            engine.run_id = 1
            engine._compute_pending(calendar[0])
            for today in calendar:
                if today not in engine.bars_by_date:
                    continue
                engine.step(today, engine.bars_by_date[today], conn)

            rows = conn.execute(
                "SELECT COUNT(*) FROM debug_snapshots WHERE run_id = 1"
            ).fetchone()
            assert rows[0] == 0, "non-debug mode should not write snapshots"
        finally:
            conn.close()

    def test_snapshot_json_has_expected_keys(self):
        """快照 JSON 包含必需的顶层键。"""
        engine, calendar = self._make_engine(debug=True)
        conn = init_backtest_db(":memory:")
        try:
            engine.run_id = 1
            engine._compute_pending(calendar[0])
            for today in calendar:
                if today not in engine.bars_by_date:
                    continue
                engine.step(today, engine.bars_by_date[today], conn)

            row = conn.execute(
                "SELECT snapshot_json FROM debug_snapshots "
                "WHERE run_id = 1 ORDER BY date LIMIT 1"
            ).fetchone()
            assert row is not None
            snap = json.loads(row[0])
            expected_keys = {"date", "account", "pending",
                             "holdings_detail", "bars_subset"}
            assert expected_keys <= set(snap.keys()), \
                f"Missing keys: {expected_keys - set(snap.keys())}"
            assert "cash" in snap["account"]
            assert "total_value" in snap["account"]
        finally:
            conn.close()


class TestReplayDefaultRun:
    """replay.py --run-id 缺省取最新 run（与其他 CLI 一致）。"""

    @staticmethod
    def _run_engine(db_path, start, end):
        from btcore.strategy import Strategy

        class SimpleDebugStrategy(Strategy):
            REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]

            def __init__(self, **kw):
                super().__init__(config=kw.pop("config", {}), **kw)

            def on_start(self, provider, first_date, end_date=None):
                pass

            def select(self, bars, snapshot, provider) -> dict:
                return {"buy": [], "sell": []}

            def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
                return []

        provider = DataProvider(MockDataBackend())
        strategy = SimpleDebugStrategy(
            config={"max_positions": 5, "initial_capital": 100000},
        )
        engine = Engine(
            strategy=strategy, provider=provider, db_path=db_path,
            initial_capital=100000, debug=True,
        )
        engine.run(start, end)

    def test_default_uses_latest_run(self, tmp_path):
        import subprocess
        import sys

        db_path = str(tmp_path / "replay.db")
        self._run_engine(db_path, "20240603", "20240605")
        self._run_engine(db_path, "20240606", "20240607")

        # 缺省 → 最新 run（20240606~07）
        r = subprocess.run(
            [sys.executable, "scripts/replay.py", db_path, "--list-symbols"],
            capture_output=True, text=True,
        )
        assert r.returncode == 0
        assert "20240606" in r.stdout
        assert "20240603" not in r.stdout

        # 显式 run 1 → 旧 run
        r1 = subprocess.run(
            [sys.executable, "scripts/replay.py", db_path, "--run-id", "1",
             "--list-symbols"],
            capture_output=True, text=True,
        )
        assert "20240603" in r1.stdout
        assert "20240606" not in r1.stdout
