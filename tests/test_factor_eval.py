"""因子评估工具单元测试 — 纯函数（calc_ic, calc_layered_returns, summarize_ic）。"""

import numpy as np
import pandas as pd
import pytest

from research.factor_eval import (
    _fmt_number,
    calc_factor_corr,
    calc_ic,
    calc_ic_decay,
    calc_layered_returns,
    summarize_ic,
)


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
        # 空序列 = 无数据，不是 IC=0（否则坍缩/无截面因子会得到伪统计量）
        assert all(
            pd.isna(s[k])
            for k in ("ic_mean", "ic_std", "icir", "ic_positive_ratio")
        )


class TestFmtNumber:
    def test_nan_and_none_show_placeholder(self):
        """无数据显示 '—'（与 summarize_ic 空序列 NaN 配套）。"""
        assert _fmt_number(float("nan")) == "—"
        assert _fmt_number(None) == "—"

    def test_number_keeps_four_decimals(self):
        assert _fmt_number(0.123456) == "0.1235"


class TestCalcFactorCorr:
    def _panel(self, n_sym=20, n_days=10, seed=7):
        rng = np.random.default_rng(seed)
        days = [f"2024010{d}" for d in range(1, 10)] + ["20240110"]
        syms = [f"S{i}" for i in range(n_sym)]
        idx = pd.MultiIndex.from_product(
            [days, syms], names=["trade_date", "symbol"]
        )
        a = rng.normal(size=len(idx))
        b = a * 0.5 + rng.normal(scale=0.5, size=len(idx))
        return pd.DataFrame({"A": a, "B": b}, index=idx), days

    def test_constant_day_skips_only_that_day(self):
        """F-FEV-01：某因子当日截面退化为常量（std=0 → 当日 pair corr NaN）
        只跳过该日该对，均值仍等于其余日的逐日 corr 均值，不得整对变 NaN。"""
        df, days = self._panel()
        df.loc[("20240104", slice(None)), "B"] = 0.0
        mat = calc_factor_corr(df)
        # 独立期望：np.corrcoef 逐日手算再取均值（跳过常量日 20240104）
        daily = [
            np.corrcoef(df.loc[d, "A"], df.loc[d, "B"])[0, 1]
            for d in days if d != "20240104"
        ]
        assert len(daily) == 9
        assert mat.loc["A", "B"] == pytest.approx(float(np.mean(daily)))

    def test_all_nan_day_matches_constant_day_skip(self):
        """整列缺失日（dropna 跳过整日）与常量日（只跳该对）语义一致。"""
        df, _ = self._panel()
        df_const = df.copy()
        df_const.loc[("20240104", slice(None)), "B"] = 0.0
        df_nan = df.copy()
        df_nan.loc[("20240104", slice(None)), "B"] = np.nan
        assert calc_factor_corr(df_const).loc["A", "B"] == pytest.approx(
            calc_factor_corr(df_nan).loc["A", "B"]
        )

    def test_pair_with_no_valid_day_is_nan(self):
        """全窗口常量因子无任何有效日 → 该对 NaN（文档：缺有效日为 NaN）。"""
        df, _ = self._panel()
        df["C"] = 1.0
        mat = calc_factor_corr(df)
        assert pd.isna(mat.loc["A", "C"])
        assert pd.isna(mat.loc["C", "A"])
        assert mat.loc["A", "B"] == pytest.approx(calc_factor_corr(
            df[["A", "B"]]).loc["A", "B"])


def test_calc_ic_decay_rejects_nonpositive_horizon():
    """F-FEV-02：horizon ≤ 0 会把 shift(-h) 反转为回顾口径，fail-fast 拒绝。"""
    s = pd.Series(dtype=float)
    with pytest.raises(ValueError, match="正整数"):
        calc_ic_decay(s, s, [-1, 1])
    with pytest.raises(ValueError, match="正整数"):
        calc_ic_decay(s, s, [0])
