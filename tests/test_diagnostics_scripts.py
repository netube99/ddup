"""scripts/diagnostics 一次性证据脚本的参数回归（F-DIAG-01）。

review_evidence_20261012.py 的 shadow 复核依赖行情库，路径此前写死为本机
私有绝对路径；现可由 --market 或 DDUP_MARKET_DB 覆盖，硬编码值只作本机
回退（跨机复现历史审查命令的前提）。
"""

import sys

import scripts.diagnostics.review_evidence_20261012 as rev

_LOCAL_FALLBACK = "/home/netube/aiwork/tushare_db/data/market.duckdb"


def _run(monkeypatch, argv):
    monkeypatch.setattr(rev, "reproduce", lambda: {})
    monkeypatch.setattr(sys, "argv", ["review_evidence.py", *argv])
    return rev.main()


def _capture_shadow(monkeypatch):
    captured = {}

    def fake_shadow(result_path, market_path):
        captured["result"] = result_path
        captured["market"] = market_path
        return {"days": 1}

    monkeypatch.setattr(rev, "shadow", fake_shadow)
    return captured


def test_market_flag_overrides_hardcoded_path(tmp_path, monkeypatch):
    captured = _capture_shadow(monkeypatch)
    rc = _run(monkeypatch, [
        "--result", str(tmp_path / "r.duckdb"),
        "--market", str(tmp_path / "m.duckdb"),
    ])
    assert rc == 0
    assert captured["market"] == str(tmp_path / "m.duckdb")


def test_market_env_var_default(tmp_path, monkeypatch):
    captured = _capture_shadow(monkeypatch)
    monkeypatch.setenv("DDUP_MARKET_DB", str(tmp_path / "env.duckdb"))
    rc = _run(monkeypatch, ["--result", str(tmp_path / "r.duckdb")])
    assert rc == 0
    assert captured["market"] == str(tmp_path / "env.duckdb")


def test_market_local_fallback_without_override(tmp_path, monkeypatch):
    captured = _capture_shadow(monkeypatch)
    monkeypatch.delenv("DDUP_MARKET_DB", raising=False)
    rc = _run(monkeypatch, ["--result", str(tmp_path / "r.duckdb")])
    assert rc == 0
    assert captured["market"] == _LOCAL_FALLBACK
