"""factor_eval CLI 口径测试 — warmup / 坍缩因子 / PIT 过滤。

run_eval 以 MockDataBackend 注入，验证与引擎同源的物化口径：
- warmup 前伸：窗口头部滚动因子有值（不再静默 NaN）
- 坍缩因子走全市场流式 compute_breadth
- --universe 走 point-in-time 成分过滤
"""

import sys

import numpy as np
import pandas as pd
import pytest

from research.factor_eval import calc_ic_decay, run_eval
from tests.conftest import MockDataBackend


@pytest.fixture
def backend():
    return MockDataBackend()


_LIB_YAML = """\
factors:
  gold_ma20:
    expr: "close_hfq / ma(close_hfq, 20) - 1"
    description: "临时本地因子库因子"
"""


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
    monkeypatch.setattr(
        "research.factor_eval.load_library", lambda path=None: fake_lib,
    )
    rc = run_eval(backend, ["parent"], "20240603", "20240628")
    captured = capsys.readouterr()
    assert rc == 1
    assert "嵌套坍缩" in captured.out + captured.err


def test_collapse_factor_skips_cross_sectional_ic(backend, capsys):
    """坍缩因子无截面变异：IC/分层区跳过并提示，不输出伪统计量。"""
    rc = run_eval(backend, ["pct_above_ma20"], "20240603", "20240628")
    out = capsys.readouterr().out
    assert rc == 0
    assert "坍缩因子不做截面 IC（时序含义，见 docs §16.5）" in out

    ic_part = out.split("IC 汇总")[1].split("分层回测")[0]
    ic_lines = [ln for ln in ic_part.splitlines() if "pct_above_ma20" in ln]
    assert ic_lines
    assert all("IR=" not in ln and "Win=" not in ln for ln in ic_lines)

    layered_part = out.split("分层回测")[1]
    layered_lines = [ln for ln in layered_part.splitlines() if "pct_above_ma20" in ln]
    assert layered_lines
    assert all("Q1" not in ln for ln in layered_lines)
    # 不崩溃即为回归（旧版输出 IR=-0.0552/Win=0.4581 的浮点噪声伪统计量）


def test_collapse_and_panel_factor_mixed(backend, capsys):
    """混合评估：保形因子照常出 IC/分层，坍缩因子被跳过。"""
    rc = run_eval(backend, ["pct_above_ma20", "mom5"], "20240603", "20240628")
    out = capsys.readouterr().out
    assert rc == 0
    ic_part = out.split("IC 汇总")[1].split("分层回测")[0]
    mom_lines = [ln for ln in ic_part.splitlines() if "mom5" in ln]
    assert mom_lines and any("IR=" in ln for ln in mom_lines)
    assert "坍缩因子不做截面 IC（时序含义，见 docs §16.5）" in out


def test_collapse_factor_decay_mode_skips(backend, capsys):
    """--decay 模式同样跳过坍缩因子，不打印伪 RankIC 行。"""
    rc = run_eval(
        backend, ["pct_above_ma20"], "20240603", "20240628", decay="1,5",
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "坍缩因子不做截面 IC（时序含义，见 docs §16.5）" in out
    assert not any(
        "pct_above_ma20" in ln and "RankIC" in ln for ln in out.splitlines()
    )


def test_local_factor_library(backend, capsys, tmp_path):
    """--factor-library：策略本地因子库可评估（GOLD-08）。"""
    lib = tmp_path / "factors.yaml"
    lib.write_text(_LIB_YAML)
    rc = run_eval(
        backend, ["gold_ma20"], "20240603", "20240628",
        library_path=str(lib),
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "gold_ma20" in out
    assert "IC 汇总" in out


def test_local_factor_library_unknown_rejected(backend, capsys, tmp_path):
    """本地库不含的因子仍 fail-fast 且提示可用因子。"""
    lib = tmp_path / "factors.yaml"
    lib.write_text(_LIB_YAML)
    rc = run_eval(
        backend, ["mom5"], "20240603", "20240628", library_path=str(lib),
    )
    captured = capsys.readouterr()
    assert rc == 1
    assert "未知因子 'mom5'" in captured.out + captured.err
    assert "gold_ma20" in captured.out + captured.err


def test_cli_factor_library_flag(backend, capsys, tmp_path, monkeypatch):
    """scripts/factor_eval.py --factor-library 端到端（临时库评估成功）。"""
    import scripts.factor_eval as cli

    class _ClosableBackend(MockDataBackend):
        def close(self):
            pass

    monkeypatch.setattr(
        cli.cli_common, "make_provider",
        lambda: type("_P", (), {"backend": _ClosableBackend()})(),
    )
    lib = tmp_path / "factors.yaml"
    lib.write_text(_LIB_YAML)
    monkeypatch.setattr(sys, "argv", [
        "factor_eval.py", "gold_ma20",
        "--start", "20240603", "--end", "20240628",
        "--factor-library", str(lib),
    ])
    rc = cli.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "gold_ma20" in out
    assert "IC 汇总" in out


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


def test_run_eval_next_open_exec_price(backend, capsys):
    """--exec-price next-open：可交易口径（T+1 开盘买入）正常跑通并标注。"""
    rc = run_eval(backend, ["mom5"], "20240603", "20240628",
                  exec_price="next-open")
    out = capsys.readouterr().out
    assert rc == 0
    assert "可交易口径 T+1开盘买" in out
    assert "IC 汇总" in out


def test_calc_ic_decay_open_hfq():
    """calc_ic_decay 带 open_hfq：前瞻从 T+1 开盘计，排名与 close 口径不同。"""
    dates = ["20240603", "20240604", "20240605", "20240606",
             "20240607", "20240610", "20240611"]
    syms = [f"S{i}" for i in range(8)]
    idx = pd.MultiIndex.from_product([dates, syms],
                                     names=["trade_date", "symbol"])
    rng = np.random.RandomState(7)
    close = pd.Series(rng.uniform(10, 20, len(idx)), index=idx)
    # 因子 = 次日收盘收益（close 口径下理应高 IC）；显式 groupby shift
    factor = close.groupby("symbol").shift(-1) / close - 1
    # 开盘 = 前收 × 随机折价 → open 口径前瞻与因子排名解耦
    prev_close = close.groupby("symbol").shift(1)
    open_ = prev_close * rng.uniform(0.9, 1.0, len(idx))
    t_close = calc_ic_decay(factor, close, [1, 3])
    t_open = calc_ic_decay(factor, close, [1, 3], open_hfq=open_)
    # close 口径 IC 显著为正（因子=次日收益），open 口径被随机折价稀释
    assert t_close["rank_ic_mean"].loc[1] > 0.5
    assert t_open["rank_ic_mean"].loc[1] < t_close["rank_ic_mean"].loc[1]
    # 开盘恒等于前收时两口径一致（退化检验）
    t_same = calc_ic_decay(factor, close, [1], open_hfq=prev_close)
    assert np.allclose(t_same["rank_ic_mean"].iloc[0],
                       t_close["rank_ic_mean"].loc[1],
                       equal_nan=True)


def test_calc_ic_decay_index_order_independent():
    """回归：扁平 shift 依赖索引序的历史 bug——(date,symbol) 与 (symbol,date)
    面板必须给出相同衰减表（公开 API 声明输入为 (trade_date, symbol)）。"""
    dates = ["20240603", "20240604", "20240605", "20240606", "20240607"]
    syms = [f"S{i}" for i in range(6)]
    rng = np.random.RandomState(11)
    idx_date = pd.MultiIndex.from_product([dates, syms],
                                          names=["trade_date", "symbol"])
    vals = pd.Series(rng.uniform(10, 20, len(idx_date)), index=idx_date)
    factor_d = pd.Series(rng.uniform(0, 1, len(idx_date)), index=idx_date)
    # 同一组 (date, symbol) 数据换成 symbol-major 序
    vals_s = vals.swaplevel(0, 1).sort_index()
    factor_s = factor_d.swaplevel(0, 1).sort_index()
    t_d = calc_ic_decay(factor_d, vals, [1, 2])
    t_s = calc_ic_decay(factor_s, vals_s, [1, 2])
    assert np.allclose(t_d["rank_ic_mean"].values,
                       t_s["rank_ic_mean"].values, equal_nan=True)
    assert np.allclose(t_d["ic_mean"].values, t_s["ic_mean"].values,
                       equal_nan=True)
