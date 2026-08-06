"""模型训练 — 时间序切分 + embargo，XGBoost 回归（panel）/ 分类（holding）。

切分纪律：按 trade_date 排序 80/20，切点之前 horizon 个交易日从训练集
剔除（embargo），防止标签窗口跨切分点重叠造成泄露。scaler 只在训练段
拟合（缺失感知：nanmean/nanstd）。缺失值在 scaler 之后填 0（= 训练段
均值，中性），与推理侧 runtime._run_batch 的口径严格一致。early
stopping 用训练段尾部 15% 日期做验证集，测试集只用于评估。
"""

import logging
import warnings
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from btcore.ml import metrics as ml_metrics
from btcore.ml import scaler as ml_scaler

logger = logging.getLogger(__name__)


@dataclass
class TrainResult:
    model: object
    scaler_mean: list[float] = field(default_factory=list)
    scaler_std: list[float] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    n_train: int = 0
    n_test: int = 0


def time_split_masks(
    dates: pd.Series, horizon: int, test_ratio: float = 0.2
) -> tuple[np.ndarray, np.ndarray]:
    """时间序切分 + embargo，返回 (train_mask, test_mask) 布尔数组。

    测试集 = 最末 test_ratio 比例的交易日；训练集剔除切点之前
    horizon 个交易日（标签窗口与测试期重叠的样本）。
    """
    uniq = np.sort(dates.unique())
    boundary_idx = int(len(uniq) * (1 - test_ratio))
    train_cut_idx = boundary_idx - horizon
    if train_cut_idx < 1:
        raise ValueError(
            f"交易日数 {len(uniq)} 不足以在 horizon={horizon} 下切分"
        )
    train_end = uniq[train_cut_idx - 1]
    test_start = uniq[boundary_idx]
    return (dates <= train_end).to_numpy(), (dates >= test_start).to_numpy()


def _fit_scaler(x_train: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """缺失感知 StandardScaler，返回 (mean, std)。

    nanmean/nanstd 只在非缺失值上拟合；±inf 归一为 NaN（除零因子表达式），
    与 build_panel 的清洗同口径；全缺失或零方差列回退 mean=0/std=1。
    """
    x_train = np.where(np.isinf(x_train), np.nan, x_train)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # 全缺失列的 nanmean 告警
        mean = np.nanmean(x_train, axis=0)
        std = np.nanstd(x_train, axis=0)
    mean = np.where(np.isnan(mean), 0.0, mean)
    std = np.where(np.isnan(std) | (std < 1e-10), 1.0, std)
    return mean, std


def _scale(x: np.ndarray, mean: np.ndarray, std: np.ndarray) -> np.ndarray:
    """标准化并在其后把缺失/±inf 填 0（= 训练段均值），与推理侧同一实现。

    数学本体在 btcore.ml.scaler.standardize（CONS-07/DUP-07：训练、推理、
    导出校验三侧收敛为同一函数，任何一侧不得手写第二份）。
    """
    return ml_scaler.standardize(x, mean, std)


def _split_train_val(dates: pd.Series, val_ratio: float = 0.15):
    """训练段内再按日期切出尾部验证集（early stopping 用）。"""
    uniq = np.sort(dates.unique())
    val_start = uniq[int(len(uniq) * (1 - val_ratio))]
    return (dates < val_start).to_numpy(), (dates >= val_start).to_numpy()


def train_panel(
    panel: pd.DataFrame,
    feature_cols: list[str],
    labels: pd.DataFrame,
    horizon: int,
) -> TrainResult:
    """panel 模型：截面排名标签回归。

    Args:
        panel: build_panel 的输出（含特征列）。
        feature_cols: spec.feature_order。
        labels: labels.xs_forward_return 的输出（label / fwd_ret 列）。
        horizon: 标签前瞻天数（embargo 宽度）。
    """
    from xgboost import XGBRegressor

    df = panel[feature_cols].join(labels).dropna(subset=["label"])
    if len(df) < 500:
        raise ValueError(f"训练样本过少: {len(df)}（需要 >= 500）")
    dates = df.index.get_level_values("trade_date").to_series()

    train_mask, test_mask = time_split_masks(dates, horizon)
    # 保留 NaN：scaler 拟合缺失感知，缺失在 scaler 之后填 0
    x_all = df[feature_cols].astype(np.float64).to_numpy()
    y_all = df["label"].to_numpy()

    mean, std = _fit_scaler(x_all[train_mask])
    x_scaled = _scale(x_all, mean, std)

    x_tr, y_tr = x_scaled[train_mask], y_all[train_mask]
    tr_dates = dates[train_mask]
    fit_mask, val_mask = _split_train_val(tr_dates)

    model = XGBRegressor(
        n_estimators=300, max_depth=4, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        objective="reg:squarederror", random_state=42,
        early_stopping_rounds=30,
    )
    model.fit(x_tr[fit_mask], y_tr[fit_mask],
              eval_set=[(x_tr[val_mask], y_tr[val_mask])], verbose=False)
    logger.info("训练完成: best_iter=%d", model.best_iteration)

    x_te = x_scaled[test_mask]
    pred = pd.Series(model.predict(x_te), index=df.index[test_mask])
    y_te = df["label"].to_numpy()[test_mask]
    label_te = pd.Series(y_te, index=df.index[test_mask])
    fwd_te = df["fwd_ret"].loc[df.index[test_mask]]

    ic = ml_metrics.daily_rank_ic(pred, label_te)
    result_metrics = {
        **ml_metrics.summarize_ic(ic),
        "layered": ml_metrics.layered_returns(pred, fwd_te),
    }
    logger.info(
        "测试集 IC: mean=%.4f icir=%.3f 多空=%.4f",
        result_metrics["ic_mean"], result_metrics["icir"],
        result_metrics["layered"].get("long_short", 0.0),
    )
    return TrainResult(
        model=model,
        scaler_mean=mean.tolist(),
        scaler_std=std.tolist(),
        metrics=result_metrics,
        n_train=int(train_mask.sum()),
        n_test=int(test_mask.sum()),
    )


def train_guard(
    samples: pd.DataFrame,
    feature_cols: list[str],
    lookahead: int,
) -> TrainResult:
    """holding scope 模型：TREND_BREAK 预警二分类（语义由策略定义）。

    Args:
        samples: labels.build_guard_samples 的输出（label + 特征列 + trade_date）。
        feature_cols: spec.feature_order。
        lookahead: 标签前瞻天数（embargo 宽度）。
    """
    from xgboost import XGBClassifier

    if len(samples) < 100:
        raise ValueError(f"训练样本过少: {len(samples)}（需要 >= 100）")
    pos_rate = float(samples["label"].mean())
    if samples["label"].sum() < 20:
        raise ValueError(f"正样本过少: {int(samples['label'].sum())}（需要 >= 20）")
    dates = samples["trade_date"]

    train_mask, test_mask = time_split_masks(dates, lookahead)
    x_all = samples[feature_cols].astype(np.float64).to_numpy()
    y_all = samples["label"].to_numpy().astype(int)

    mean, std = _fit_scaler(x_all[train_mask])
    x_scaled = _scale(x_all, mean, std)

    x_tr, y_tr = x_scaled[train_mask], y_all[train_mask]
    tr_dates = dates[train_mask]
    fit_mask, val_mask = _split_train_val(tr_dates)

    spw = float((y_tr == 0).sum() / max((y_tr == 1).sum(), 1))
    model = XGBClassifier(
        n_estimators=300, max_depth=3, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8,
        scale_pos_weight=spw, eval_metric="logloss",
        random_state=42, early_stopping_rounds=30,
    )
    model.fit(x_tr[fit_mask], y_tr[fit_mask],
              eval_set=[(x_tr[val_mask], y_tr[val_mask])], verbose=False)
    logger.info("训练完成: best_iter=%d", model.best_iteration)

    from sklearn.metrics import precision_score, recall_score, roc_auc_score

    x_te, y_te = x_scaled[test_mask], y_all[test_mask]
    prob = model.predict_proba(x_te)[:, 1]
    pred_bin = (prob >= 0.5).astype(int)
    result_metrics = {
        "auc": float(roc_auc_score(y_te, prob)) if len(np.unique(y_te)) > 1 else 0.0,
        "precision@0.5": float(precision_score(y_te, pred_bin, zero_division=0)),
        "recall@0.5": float(recall_score(y_te, pred_bin, zero_division=0)),
        "pos_rate_train": pos_rate,
    }
    logger.info(
        "测试集: AUC=%.3f precision=%.3f recall=%.3f",
        result_metrics["auc"], result_metrics["precision@0.5"],
        result_metrics["recall@0.5"],
    )
    return TrainResult(
        model=model,
        scaler_mean=mean.tolist(),
        scaler_std=std.tolist(),
        metrics=result_metrics,
        n_train=int(train_mask.sum()),
        n_test=int(test_mask.sum()),
    )



