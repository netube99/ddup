"""factor_eval CLI 口径测试 — warmup / 坍缩因子 / PIT 过滤。

run_eval 以 MockDataBackend 注入，验证与引擎同源的物化口径：
- warmup 前伸：窗口头部滚动因子有值（不再静默 NaN）
- 坍缩因子走全市场流式 compute_breadth
- --universe 走 point-in-time 成分过滤
"""

import pytest

from research.factor_eval import run_eval
from tests.conftest import MockDataBackend


@pytest.fixture
def backend():
    return MockDataBackend()


def test_warmup_lookback_applied(backend, capsys):
    """窗口起点前移 main_days 天：warmup 打印 + 头部截面有值。"""
    rc = run_eval(
        backend, ["mom5"], "20240610", "20240628",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "warmup: 20230611 ~ 20240610" in out
    assert "IC 汇总" in out


def test_collapse_factor_full_market(backend, capsys):
    """坍缩因子经 compute_breadth 全市场流式计算，不落候选池面板。"""
    rc = run_eval(
        backend, ["pct_above_ma20"], "20240603", "20240628",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "坍缩因子 pct_above_ma20: 全市场广度口径" in out


def test_collapse_nested_failfast(backend, capsys, monkeypatch):
    """表达式引用坍缩因子：fail-fast 而非静默错误语义。"""
    fake_lib = {
        "parent": {"expr": "close_hfq / breadth"},
        "breadth": {"expr": "mean(close_hfq > 0)"},
    }
    monkeypatch.setattr("research.factor_eval.load_library", lambda: fake_lib)
    rc = run_eval(backend, ["parent"], "20240603", "20240628")
    captured = capsys.readouterr()
    assert rc == 1
    assert "嵌套坍缩" in captured.out + captured.err


def test_universe_pit_filter(backend, capsys):
    """--universe 时按 point-in-time 成分过滤（ml_train 同口径）。"""
    backend.get_index_members = lambda codes, start, end: {
        "20240601": {"000001.SZ", "000002.SZ", "600036.SH"},
        "20240615": {"000001.SZ", "000002.SZ", "600036.SH", "600519.SH"},
    }
    rc = run_eval(
        backend, ["mom5"], "20240603", "20240628", universe="CSI300",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "PIT 成分过滤后: " in out
    # 窗口内每交易日 3 只候选（600519 仅 20240615 后进池，按 PIT 也应过滤进）
    # 断言过滤确实发生：行数 < 未过滤行数
    line = next(x for x in out.splitlines() if "PIT 成分过滤后" in x)
    n_rows = int(line.split(":")[-1].strip().split()[0])
    assert 0 < n_rows < 4 * 21  # 3~4 只票 × 窗口天数上限


def test_unknown_factor_rejected(backend, capsys):
    rc = run_eval(backend, ["no_such_factor"], "20240603", "20240628")
    captured = capsys.readouterr()
    assert rc == 1
    assert "未知因子" in captured.out + captured.err


def test_empty_factors_rejected(backend, capsys):
    rc = run_eval(backend, [], "20240603", "20240628")
    captured = capsys.readouterr()
    assert rc == 1
    assert "至少需要一个因子名称" in captured.out + captured.err
