"""ML 推理运行时 — ONNX 会话缓存、面板批量物化、决策时点持仓求值。

纯机制：只依赖 btcore.types 与 spec.ModelSpec，不 import engine/provider。
引擎不认识模型的意图（选股/离场/市场状态……），本模块也不认识——
只负责按 scope 求值并把分数交回数据管线：

  - scope=panel：preload 阶段对整个面板一次性批量推理，写 ml_<name> 列。
    输入全是因果物化列、逐行点态函数，前视安全由构造保证。
  - scope=holding：决策时点（_compute_pending）逐持仓推理，账户态特征
    （hold_days / ret_from_entry）由本模块按统一公式计算，训练侧复用同一
    函数；分数注入该持仓的 bar dict，策略自行解释。

onnxruntime 惰性 import——未配置模型的策略零依赖；配置了但缺包则
首次推理时 fail-fast（明确声明的依赖缺失，不静默降级）。
"""

import logging

import numpy as np
import pandas as pd

from btcore.ml.spec import SCOPE_PANEL, ModelSpec
from btcore.types import bar_get

logger = logging.getLogger(__name__)

_sessions: dict[str, object] = {}


def _get_session(artifact: str):
    """ONNX 会话进程级缓存（同一 artifact 多模型复用）。"""
    sess = _sessions.get(artifact)
    if sess is None:
        try:
            import onnxruntime as rt
        except ImportError as exc:
            raise RuntimeError(
                "策略配置了 models 但缺少 onnxruntime —— "
                "uv pip install onnxruntime"
            ) from exc
        sess = rt.InferenceSession(artifact, providers=["CPUExecutionProvider"])
        _sessions[artifact] = sess
    return sess


def _apply_scaler(spec: ModelSpec, arr: np.ndarray) -> np.ndarray:
    if spec.scaler_mean and len(spec.scaler_mean) == arr.shape[1]:
        mean = np.asarray(spec.scaler_mean, dtype=np.float32)
        std = np.asarray(spec.scaler_std, dtype=np.float32)
        arr = (arr - mean) / np.where(std > 1e-10, std, 1.0)
    elif spec.scaler_mean:
        logger.warning(
            "模型 %s scaler 维度 %d != 特征维度 %d，跳过缩放",
            spec.name, len(spec.scaler_mean), arr.shape[1],
        )
    return arr


def _run_batch(spec: ModelSpec, arr: np.ndarray) -> np.ndarray:
    """批量推理，返回 1-D 分数：分类取正类概率，回归取预测值。"""
    arr = _apply_scaler(spec, arr.astype(np.float32, copy=False))
    sess = _get_session(spec.artifact)
    input_name = sess.get_inputs()[0].name
    outputs = sess.run(None, {input_name: arr})
    raw = outputs[1] if len(outputs) > 1 else outputs[0]
    if isinstance(raw, np.ndarray) and raw.ndim == 2 and raw.shape[1] > 1:
        return raw[:, 1].astype(np.float64)  # 分类：正类概率列
    if isinstance(raw, np.ndarray):
        return raw.flatten().astype(np.float64)  # 回归：单值输出
    raise RuntimeError(
        f"模型 {spec.name} ONNX 输出形态不支持: {type(raw)} —— "
        "请用 scripts/ml_train.py 导出的模型"
    )


def _apply_post_transform(scores: pd.Series, transform: str) -> pd.Series:
    """截面后变换（按 trade_date 分组，仅在物化面板的截面上进行）。"""
    if transform == "xs_rank":
        return scores.groupby(level="trade_date", sort=False).rank(pct=True)
    if transform == "xs_zscore":
        g = scores.groupby(level="trade_date", sort=False)
        std = g.transform("std")
        z = (scores - g.transform("mean")) / std.where(std > 1e-10)
        return z.fillna(0.0)
    return scores


def materialize_predictions(bars_df: pd.DataFrame, specs: list[ModelSpec]) -> None:
    """panel 模型批量推理，原地写 ml_<name> 列并应用截面后变换。

    在引擎因子物化之后、factor_universe 裁切之前调用：截面后变换的
    排名口径 = 因子计算域，与训练面板口径一致（select 侧评分另有
    eval_factor_specs 的截面 rank，不冲突）。
    """
    for spec in specs:
        if spec.scope != SCOPE_PANEL:
            continue  # holding scope：特征依赖账户状态，决策时点才求值
        cols = spec.features + spec.raw_features
        missing = [c for c in cols if c not in bars_df.columns]
        if missing:
            raise ValueError(
                f"模型 {spec.name} 特征列未物化: {missing} —— "
                "loader 闭包合并应已覆盖，请检查 models 配置"
            )
        features = bars_df[cols].astype(np.float64).fillna(0.0).to_numpy()
        scores = pd.Series(_run_batch(spec, features), index=bars_df.index)
        bars_df[spec.column] = _apply_post_transform(scores, spec.post_transform)
        logger.debug(
            "模型 %s 物化完成: %d 行, post_transform=%s",
            spec.name, len(scores), spec.post_transform,
        )


def apply_post_transform_flat(scores: pd.Series, transform: str) -> pd.Series:
    """平坦 Series 的截面后变换（holding scope 决策时点的当日持仓截面）。"""
    if scores.empty:
        return scores
    if transform == "xs_rank":
        return scores.rank(pct=True)
    if transform == "xs_zscore":
        std = scores.std()
        if std > 1e-10:
            return (scores - scores.mean()) / std
        return scores * 0.0
    return scores


def compute_state_features(names: list[str], bar: dict, holding) -> dict[str, float]:
    """账户态特征统一计算公式（训练侧重放与引擎推理共用）。

    hold_days: 引擎维护的持仓交易日数。
    ret_from_entry: 当日 hfq 收盘 / 买入均价 - 1（hfq 口径，与排名一致）。
    """
    out: dict[str, float] = {}
    for n in names:
        if n == "hold_days":
            out[n] = float(holding.holding_days)
        elif n == "ret_from_entry":
            close = bar_get(bar, "close_hfq", None) or bar_get(bar, "close", None)
            ep = holding.entry_price
            out[n] = (float(close) / ep - 1.0) if (close and ep > 0) else 0.0
        else:
            raise ValueError(f"未知 state feature: {n!r}")
    return out


def holding_score(spec: ModelSpec, bar: dict, holding) -> float | None:
    """holding scope 模型对单个持仓推理；特征缺失过半返回 None。"""
    state = compute_state_features(spec.state_features, bar, holding)
    values: list[float] = []
    nan_count = 0
    for name in spec.feature_order:
        v = state.get(name)
        if v is None:
            v = bar_get(bar, name, None)
        if v is None or (isinstance(v, float) and np.isnan(v)):
            values.append(0.0)
            nan_count += 1
        else:
            values.append(float(v))
    if nan_count > len(values) * 0.5:
        return None
    arr = np.array([values], dtype=np.float32)
    return float(_run_batch(spec, arr)[0])
