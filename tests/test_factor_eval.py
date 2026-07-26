"""因子评估工具单元测试 — 纯函数（calc_ic, calc_layered_returns, summarize_ic）。"""

import numpy as np
import pandas as pd
import pytest

from research.factor_eval import calc_ic, calc_layered_returns, summarize_ic


def _make_series(data_dict: dict[tuple[str, str], float]) -> pd.Series:
    """构造 (date, symbol) MultiIndex Series。"""
    idx = pd.MultiIndex.from_tuples(data_dict.keys(), names=["trade_date", "symbol"])
    return pd.Series(list(data_dict.values()), index=idx)


class TestCalcIC:
    def test_perfect_positive_ic(self):
        fv = _make_series({
            ("20240101", "A"): 1.0, ("20240101", "B"): 2.0, ("20240101", "C"): 3.0,
        })
        fr = _make_series({
            ("20240101", "A"): 0.01, ("20240101", "B"): 0.02, ("20240101", "C"): 0.03,
        })
        ic, ric = calc_ic(fv, fr)
        assert len(ic) == 1
        assert ic.iloc[0] == pytest.approx(1.0, abs=0.01)
        assert ric.iloc[0] == pytest.approx(1.0, abs=0.01)

    def test_perfect_negative_ic(self):
        fv = _make_series({
            ("20240101", "A"): 1.0, ("20240101", "B"): 2.0, ("20240101", "C"): 3.0,
        })
        fr = _make_series({
            ("20240101", "A"): 0.03, ("20240101", "B"): 0.02, ("20240101", "C"): 0.01,
        })
        ic, ric = calc_ic(fv, fr)
        assert ic.iloc[0] == pytest.approx(-1.0, abs=0.01)

    def test_random_ic(self):
        rng = np.random.default_rng(42)
        fv = _make_series({
            ("20240101", f"S{i:03d}"): rng.standard_normal()
            for i in range(50)
        })
        fr = _make_series({
            ("20240101", f"S{i:03d}"): rng.standard_normal()
            for i in range(50)
        })
        ic, ric = calc_ic(fv, fr)
        # 独立随机数据 IC 应接近 0（实测 |ic| < 0.04，上界取 0.3 留足余量）
        assert abs(ic.iloc[0]) < 0.3
        assert abs(ric.iloc[0]) < 0.3

    def test_multi_date(self):
        fv = _make_series({
            ("20240101", "A"): 1.0, ("20240101", "B"): 2.0, ("20240101", "C"): 3.0,
            ("20240102", "A"): 3.0, ("20240102", "B"): 2.0, ("20240102", "C"): 1.0,
        })
        fr = _make_series({
            ("20240101", "A"): 0.01, ("20240101", "B"): 0.02, ("20240101", "C"): 0.03,
            ("20240102", "A"): 0.03, ("20240102", "B"): 0.02, ("20240102", "C"): 0.01,
        })
        ic, ric = calc_ic(fv, fr)
        assert len(ic) == 2
        assert ic.iloc[0] == pytest.approx(1.0, abs=0.01)
        assert ic.iloc[1] == pytest.approx(1.0, abs=0.01)

    def test_small_group_skipped(self):
        """少于 3 个样本的日子返回 NaN。"""
        fv = _make_series({
            ("20240101", "A"): 1.0, ("20240101", "B"): 2.0,
        })
        fr = _make_series({
            ("20240101", "A"): 0.01, ("20240101", "B"): 0.02,
        })
        ic, ric = calc_ic(fv, fr)
        assert np.isnan(ic.iloc[0])


class TestCalcLayeredReturns:
    def test_three_quantiles(self):
        fv = _make_series({
            ("20240101", "A"): 1.0, ("20240101", "B"): 2.0, ("20240101", "C"): 3.0,
            ("20240101", "D"): 4.0, ("20240101", "E"): 5.0, ("20240101", "F"): 6.0,
        })
        fr = _make_series({
            ("20240101", "A"): -0.03, ("20240101", "B"): -0.02, ("20240101", "C"): 0.01,
            ("20240101", "D"): 0.02, ("20240101", "E"): 0.03, ("20240101", "F"): 0.05,
        })
        result = calc_layered_returns(fv, fr, n_quantiles=3)
        assert 1 in result and 3 in result
        # Q3 (highest factor = E,F) should have higher return than Q1 (lowest = A,B)
        q3_ret = result[3].iloc[0] - 1.0
        q1_ret = result[1].iloc[0] - 1.0
        assert q3_ret > q1_ret


class TestSummarizeIC:
    def test_positive_ic(self):
        ic = pd.Series([0.05, 0.03, 0.07, 0.01, 0.04])
        s = summarize_ic(ic)
        assert s["ic_positive_ratio"] == 1.0
        assert s["icir"] > 0
        assert s["n_days"] == 5

    def test_mixed_ic(self):
        ic = pd.Series([0.05, -0.02, 0.03, -0.01, 0.01])
        s = summarize_ic(ic)
        assert s["ic_positive_ratio"] == 0.6
        assert s["n_days"] == 5

    def test_empty(self):
        s = summarize_ic(pd.Series(dtype=float))
        assert s["n_days"] == 0
        assert s["ic_mean"] == 0.0
