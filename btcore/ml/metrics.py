"""ML 模型评估指标 — 与 research/factor_eval 同口径，自包含实现。

不 import research（守 btcore 分层规则）：研究侧 interactive 分析仍走
research/factor_eval.py，训练侧在这里内置同一套指标定义。
"""

import numpy as np
import pandas as pd


def daily_rank_ic(pred: pd.Series, label: pd.Series) -> pd.Series:
    """每日截面 Spearman IC 序列。

    pred / label 均为 (trade_date, symbol) MultiIndex，逐日 rank 后 pearson。
    截面样本 < 5 的日期返回 NaN。
    """
    df = pd.DataFrame({"p": pred, "y": label}).dropna()
    if df.empty:
        return pd.Series(dtype=float)

    def _ic(g):
        if len(g) < 5:
            return np.nan
        return g["p"].rank().corr(g["y"].rank())

    return df.groupby(level="trade_date", sort=False).apply(_ic).dropna()


def summarize_ic(ic: pd.Series) -> dict:
    """IC 汇总：mean / std / ICIR / 正值占比 / 有效天数。"""
    ic = ic.dropna()
    if ic.empty:
        return {"ic_mean": 0.0, "ic_std": 0.0, "icir": 0.0,
                "ic_pos_ratio": 0.0, "n_days": 0}
    std = float(ic.std())
    return {
        "ic_mean": float(ic.mean()),
        "ic_std": std,
        "icir": float(ic.mean() / std) if std > 1e-12 else 0.0,
        "ic_pos_ratio": float((ic > 0).mean()),
        "n_days": int(len(ic)),
    }


def layered_returns(
    pred: pd.Series, fwd_ret: pd.Series, n_layers: int = 10
) -> dict:
    """十分层多空：按 pred 每日截面分 n 层，返回各层平均前向收益 + 多空差。

    Returns:
        {"layer_mean": {层号: 平均前向收益}, "long_short": 顶层-底层,
         "monotonic": 层均值是否单调递增}
    """
    df = pd.DataFrame({"p": pred, "r": fwd_ret}).dropna()
    if df.empty:
        return {"layer_mean": {}, "long_short": 0.0, "monotonic": False}

    def _layer(g):
        if len(g) < n_layers * 2:
            return pd.Series(np.nan, index=g.index)
        return pd.qcut(g, n_layers, labels=False, duplicates="drop").astype(float)

    df["layer"] = df.groupby(level="trade_date", sort=False)["p"].transform(_layer)
    df = df.dropna(subset=["layer"])
    if df.empty:
        return {"layer_mean": {}, "long_short": 0.0, "monotonic": False}
    layer_mean = df.groupby("layer")["r"].mean()
    if len(layer_mean) < 2:
        return {"layer_mean": {}, "long_short": 0.0, "monotonic": False}
    means = {int(k): float(v) for k, v in layer_mean.items()}
    top, bottom = layer_mean.iloc[-1], layer_mean.iloc[0]
    return {
        "layer_mean": means,
        "long_short": float(top - bottom),
        "monotonic": bool(layer_mean.is_monotonic_increasing),
    }
