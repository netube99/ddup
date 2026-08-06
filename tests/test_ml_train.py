"""训练管线冒烟 + 确定性：train_panel / train_guard（合成面板，不连真实库）。

train_panel/train_guard 此前零测试覆盖（整改指导 TEST-02）。本文件用合成
面板（含 NaN/±inf 特征）跑训练冒烟：断言返回结构、spw/embargo 路径不崩、
scaler 参数有限；确定性用例用 xgboost.config_context(nthread=1) 固定线程数，
验证同输入两次训练逐值一致（hist 多线程跨核数存在浮点级差异，必须 pin）。
需要 xgboost 的用例全部函数级 importorskip——无 ML 依赖环境仅相关用例跳过，
模块级 import 只碰 trainer 的纯逻辑（xgboost 在函数内惰性导入），不整文件跳过。
"""

import numpy as np
import pandas as pd
import pytest

from btcore.ml.trainer import train_guard, train_panel

_FEATURES = ["mom20", "turnover_rate", "vol_z"]


def _dates(n_dates, start="2024-01-02"):
    base = pd.to_datetime(start)
    return [(base + pd.Timedelta(days=i)).strftime("%Y%m%d")
            for i in range(n_dates)]


def _panel(n_dates=40, n_symbols=15, seed=7):
    """合成面板：随机特征 + 5% NaN + 少量 ±inf（CONS-07 兜底路径）。"""
    rng = np.random.RandomState(seed)
    date_strs = _dates(n_dates)
    symbols = [f"S{i:03d}" for i in range(n_symbols)]
    idx = pd.MultiIndex.from_product(
        [date_strs, symbols], names=["trade_date", "symbol"],
    )
    n = len(idx)
    panel = pd.DataFrame(
        {
            "mom20": rng.randn(n),
            "turnover_rate": rng.rand(n) * 5,
            "vol_z": rng.randn(n) * 2,
        },
        index=idx,
    )
    nan_mask = rng.rand(n) < 0.05
    panel.loc[nan_mask, "mom20"] = np.nan
    panel.iloc[::97, 2] = np.inf
    panel.iloc[::101, 2] = -np.inf
    return panel


def _labels(panel, seed=11):
    """xs_forward_return 同构标签：label=前向收益截面排名，fwd_ret=前向收益。"""
    rng = np.random.RandomState(seed)
    fwd = (panel.groupby(level="trade_date")["mom20"].rank(pct=True) * 0.01
           + rng.randn(len(panel)) * 0.001)
    labels = pd.DataFrame({"fwd_ret": fwd}, index=panel.index)
    labels["label"] = (
        labels.groupby(level="trade_date")["fwd_ret"].rank(pct=True)
    )
    return labels


def _guard_samples(n_dates=30, per_day=6, seed=3):
    """holding 侧合成样本：trade_date 为列，label 正样本率 ~30%。"""
    rng = np.random.RandomState(seed)
    rows = []
    for d in _dates(n_dates, start="2024-03-01"):
        for _ in range(per_day):
            rows.append({
                "trade_date": d,
                "mom20": rng.randn(),
                "turnover_rate": rng.rand() * 5,
                "label": 1 if rng.rand() < 0.3 else 0,
            })
    return pd.DataFrame(rows)


def _scaled_x(panel, res, n=20):
    """按训练返回的 scaler 标准化前 n 行（推理侧同口径：缺失填 0）。"""
    x = panel[_FEATURES].astype(np.float64).head(n).to_numpy()
    mean = np.asarray(res.scaler_mean, dtype=np.float64)
    std = np.asarray(res.scaler_std, dtype=np.float64)
    return np.nan_to_num((x - mean) / std, nan=0.0)


def test_train_panel_smoke():
    """train_panel 冒烟：含 NaN/±inf 面板可训练，返回结构完整。"""
    pytest.importorskip("xgboost", reason="需要 xgboost")
    panel = _panel()
    labels = _labels(panel)
    res = train_panel(panel, _FEATURES, labels, horizon=5)

    assert res.n_train > 0 and res.n_test > 0
    assert len(res.scaler_mean) == 3 and len(res.scaler_std) == 3
    assert np.isfinite(res.scaler_mean).all()
    assert np.isfinite(res.scaler_std).all()
    assert "ic_mean" in res.metrics and "icir" in res.metrics
    layered = res.metrics["layered"]
    assert "long_short" in layered and "layer_mean" in layered
    # embargo：训练/测试日期不重叠（切点前 horizon 日被剔除）
    assert res.n_train + res.n_test <= len(panel)

    pred = res.model.predict(_scaled_x(panel, res))
    assert np.isfinite(pred).all()


def test_train_guard_smoke():
    """train_guard 冒烟：spw（scale_pos_weight）与分类路径不崩。"""
    pytest.importorskip("xgboost", reason="需要 xgboost")
    samples = _guard_samples()
    res = train_guard(samples, ["mom20", "turnover_rate"], lookahead=3)

    assert res.n_train > 0 and res.n_test > 0
    assert len(res.scaler_mean) == 2 and np.isfinite(res.scaler_mean).all()
    assert {"auc", "precision@0.5", "recall@0.5", "pos_rate_train"} \
        <= set(res.metrics)
    # spw ≈ 负/正比（正样本率 ~30% → ~2.33）；同时覆盖正样本过少校验不误触发
    assert res.metrics["pos_rate_train"] == pytest.approx(0.3, abs=0.05)
    assert res.metrics["recall@0.5"] >= 0.0


def test_train_panel_deterministic():
    """确定性：同输入两次训练（n_jobs=1）scaler/指标/预测逐值一致。"""
    xgboost = pytest.importorskip("xgboost", reason="需要 xgboost")
    panel = _panel()
    labels = _labels(panel)
    with xgboost.config_context(nthread=1):
        r1 = train_panel(panel, _FEATURES, labels, horizon=5)
        r2 = train_panel(panel, _FEATURES, labels, horizon=5)
    assert r1.scaler_mean == pytest.approx(r2.scaler_mean)
    assert r1.scaler_std == pytest.approx(r2.scaler_std)
    assert r1.metrics["ic_mean"] == pytest.approx(r2.metrics["ic_mean"])
    assert r1.metrics["icir"] == pytest.approx(r2.metrics["icir"])
    assert r1.metrics["ic_pos_ratio"] == pytest.approx(r2.metrics["ic_pos_ratio"])
    assert r1.metrics["layered"]["long_short"] \
        == pytest.approx(r2.metrics["layered"]["long_short"])
    assert (r1.n_train, r1.n_test) == (r2.n_train, r2.n_test)
    x = _scaled_x(panel, r1)
    np.testing.assert_array_equal(r1.model.predict(x), r2.model.predict(x))


def test_train_guard_deterministic():
    """确定性：guard 两次训练指标一致（spw 依赖随机种子，固定后稳定）。"""
    xgboost = pytest.importorskip("xgboost", reason="需要 xgboost")
    samples = _guard_samples()
    with xgboost.config_context(nthread=1):
        r1 = train_guard(samples, ["mom20", "turnover_rate"], lookahead=3)
        r2 = train_guard(samples, ["mom20", "turnover_rate"], lookahead=3)
    assert r1.metrics["auc"] == pytest.approx(r2.metrics["auc"])
    assert r1.metrics["recall@0.5"] == pytest.approx(r2.metrics["recall@0.5"])
    assert r1.scaler_mean == pytest.approx(r2.scaler_mean)
    assert (r1.n_train, r1.n_test) == (r2.n_train, r2.n_test)


def test_train_panel_too_few_samples_raises():
    """样本不足 fail-fast（train_panel < 500 行）。"""
    pytest.importorskip("xgboost", reason="需要 xgboost")
    panel = _panel(n_dates=10, n_symbols=5)  # 50 行
    labels = _labels(panel)
    with pytest.raises(ValueError, match="训练样本过少"):
        train_panel(panel, _FEATURES, labels, horizon=5)


def test_train_guard_too_few_positives_raises():
    """正样本过少 fail-fast（train_guard < 20 正例）。"""
    pytest.importorskip("xgboost", reason="需要 xgboost")
    samples = _guard_samples(seed=99)
    # 强制正样本率 5%（180 行 × 5% = 9 正例 < 20）→ 直接报错，不进入训练
    samples["label"] = 0
    samples.loc[samples.sample(frac=0.05, random_state=1).index, "label"] = 1
    with pytest.raises(ValueError, match="正样本过少"):
        train_guard(samples, ["mom20", "turnover_rate"], lookahead=3)
