"""research.composite 测试：滚动 IC/ICIR 加权合成与评估。"""

import numpy as np
import pandas as pd
import pytest

from research.composite import _xsec_zscore, combine_factors, evaluate_composite

_DATES = pd.date_range("2024-01-01", periods=120).strftime("%Y%m%d")
_SYMS = [f"S{i:02d}" for i in range(30)]


def _mk_data(seed=7, noise=0.05):
    """构造因子 A=fwd+噪声（正 IC）、B=纯噪声（零 IC）、C=-fwd+噪声（负 IC）。"""
    idx = pd.MultiIndex.from_product([_DATES, _SYMS], names=["trade_date", "symbol"])
    rng = np.random.default_rng(seed)
    fwd = pd.Series(rng.normal(0, 1, len(idx)), index=idx)
    df = pd.DataFrame({
        "A": fwd + rng.normal(0, noise, len(idx)),
        "B": rng.normal(0, 1, len(idx)),
        "C": -fwd + rng.normal(0, noise, len(idx)),
    }, index=idx)
    return df, fwd


class TestCombine:
    def test_equal_is_zscore_mean(self):
        df, fwd = _mk_data()
        comp = combine_factors(df[["A", "B"]], fwd, method="equal")
        expected = (df[["A", "B"]].apply(_xsec_zscore)).mean(axis=1)
        assert ((comp - expected).abs().fillna(0) < 1e-12).all()

    def test_icir_weight_sign_and_magnitude(self):
        """强因子权重绝对值大；负 IC 因子权重为负（方向翻转）。"""
        df, fwd = _mk_data()
        comp = combine_factors(df, fwd, method="icir", window=20)
        assert comp.notna().any()
        # 合成因子与 A 强正相关、与 C 强负相关、与 B 近无关
        d = comp.dropna().index.get_level_values("trade_date")[-1]
        last = pd.DataFrame({
            "comp": comp.loc[d], "A": df["A"].loc[d],
            "B": df["B"].loc[d], "C": df["C"].loc[d],
        })
        assert last["comp"].corr(last["A"]) > 0.9
        assert last["comp"].corr(last["C"]) < -0.9
        assert abs(last["comp"].corr(last["B"])) < 0.3

    def test_ic_method(self):
        df, fwd = _mk_data()
        comp = combine_factors(df[["A", "C"]], fwd, method="ic", window=20)
        assert comp.notna().any()

    def test_leading_nan_during_warmup(self):
        """权重需滚动窗口估计，前期 composite 为 NaN（不用当日 IC，无前视）。"""
        df, fwd = _mk_data()
        window = 30
        comp = combine_factors(df[["A"]], fwd, method="icir", window=window)
        first_valid = comp.dropna().index.get_level_values("trade_date").min()
        # min_periods=window//2 → rolling 在第 window//2-1 位首次有效，shift(1) 再顺延一日
        assert first_valid == _DATES[window // 2]

    def test_unknown_method_rejected(self):
        df, fwd = _mk_data()
        with pytest.raises(ValueError, match="未知合成方法"):
            combine_factors(df, fwd, method="magic")


class TestEvaluate:
    def test_composite_beats_noise_factor(self):
        """合成因子 IC 显著为正且远高于纯噪声因子。"""
        df, fwd = _mk_data(noise=0.1)
        comp = combine_factors(df, fwd, method="icir", window=20)
        ev = evaluate_composite(comp, fwd)
        assert ev["ic"]["ic_mean"] > 0.5
        ev_b = evaluate_composite(df["B"], fwd)
        assert ev["ic"]["ic_mean"] > abs(ev_b["ic"]["ic_mean"]) + 0.3
        assert ev["layered"]                 # 分层结果非空
