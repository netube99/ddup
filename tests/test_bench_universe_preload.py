"""bench_universe_preload.py 成分并集解析回归（F-CLI-04）。

resolve_index_universe 返回 None（后端无 get_index_members 或区间无成分
快照）在引擎侧是合法语义（=全市场），但基准对比脚本必须显式失败：
加载段此前 len(None) 裸 TypeError，端到端段静默按全市场计时失真。
"""

import sys

import scripts.bench_universe_preload as bench


class _Backend:
    def close(self):
        pass


class _Provider:
    backend = _Backend()


def test_none_universe_fails_fast(monkeypatch, capsys):
    monkeypatch.setattr(bench.cli_common, "make_provider",
                        lambda: _Provider())
    monkeypatch.setattr(bench, "resolve_index_universe", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", [
        "bench_universe_preload.py", "--start", "20240603", "--end", "20240630",
    ])
    assert bench.main() == 1
    err = capsys.readouterr().err
    assert "无成分快照" in err
    assert "成分并集基准不可用" in err


def test_universe_resolved_once_and_load_segment_runs(monkeypatch, capsys):
    """正常路径：成分并集只解析一次，加载段照常输出两组计时。"""
    calls = []

    def fake_resolve(backend, codes, start, end):
        calls.append((tuple(codes), start, end))
        return ["600000.SH", "600036.SH"]

    def fake_bench_load(backend, symbols, start, end):
        return (100, len(symbols or ["a", "b"]), 0.01, 1.0)

    monkeypatch.setattr(bench.cli_common, "make_provider",
                        lambda: _Provider())
    monkeypatch.setattr(bench, "resolve_index_universe", fake_resolve)
    monkeypatch.setattr(bench, "bench_load", fake_bench_load)
    monkeypatch.setattr(sys, "argv", [
        "bench_universe_preload.py", "--start", "20240603", "--end", "20240630",
        "--skip-engine",
    ])
    assert bench.main() == 0
    out = capsys.readouterr().out
    assert "指数成分并集: 2 只" in out
    assert "preload[全市场]" in out and "preload[成分并集]" in out
    # 两段基准共用一次解析：加载段不再重复 resolve
    assert len(calls) == 1
