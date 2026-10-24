"""核心持久化链路（DuckDB）审查回归测试。

2026-10-24 实盘 V2 重制迁移说明（docs/live_redesign.md §7）：
- TestLedgerDerivedRunId / TestLedgerRunsEndDate：V1 账本"重写 runs/
  trade_log 衍生表"的路径已整体移除（V2 无回测衍生表），契约主体不存在；
  runs 表通用行为由 btcore.database 与回测链路测试覆盖。
- TestSyncFillDateDefault / TestInitAtomicity：等价保护迁移至
  tests/test_live_account.py（statement 缺省成交日、create/导入原子性）
  与 tests/test_live.py（CLI 错误输入不留半账本）。
- TestSweepLegacyFormat 与实盘无关，原样保留。
"""

import sqlite3
import sys

import pytest
import yaml


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
