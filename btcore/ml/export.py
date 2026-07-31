"""ONNX 导出 — model + scaler + meta v2 落盘，sklearn/ONNX 输出一致性校验。

meta v2 是训练与推理的特征契约（见 btcore.ml.spec.ModelSpec）：
feature_order = factors + raw + state_features，双方严格按此列序取向量。
"""

import hashlib
import json
import logging
from pathlib import Path

import numpy as np

from btcore.ml import runtime as ml_runtime
from btcore.ml.spec import META_VERSION, ModelSpec
from btcore.ml.trainer import TrainResult

logger = logging.getLogger(__name__)


def export_model(
    result: TrainResult,
    spec: ModelSpec,
    out_path: str,
    *,
    label: dict,
    train_window: list[str],
    verify_rows: np.ndarray | None = None,
) -> tuple[str, str]:
    """导出 ONNX 模型 + meta v2，返回 (onnx_path, meta_path)。

    Args:
        result: trainer 的训练产物。
        spec: 训练时的 ModelSpec（特征契约来源，可来自 YAML 引导）。
        out_path: ONNX 输出路径（一般为 YAML artifact 声明的路径）。
        label: 标签描述（{"type": "xs_fwdret", "horizon": 5} 等）。
        train_window: [start, end]。
        verify_rows: 用于 ONNX/sklearn 一致性校验的原始特征样本（未缩放）。
    """
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType

    n_features = len(spec.feature_order)
    initial_type = [("float_input", FloatTensorType([None, n_features]))]
    onx = convert_xgboost(result.model, initial_types=initial_type, target_opset=15)

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    blob = onx.SerializeToString()
    out.write_bytes(blob)

    meta = {
        "version": META_VERSION,
        "name": spec.name,
        "scope": spec.scope,
        "features": {"factors": list(spec.features), "raw": list(spec.raw_features)},
        "state_features": list(spec.state_features),
        "post_transform": spec.post_transform,
        "label": label,
        "train_window": list(train_window),
        "scaler_mean": list(result.scaler_mean),
        "scaler_std": list(result.scaler_std),
        "metrics": result.metrics,
        "n_train": result.n_train,
        "n_test": result.n_test,
        "artifact_sha256": hashlib.sha256(blob).hexdigest(),
    }

    meta_path = out.with_suffix(".meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    # 一致性校验：同一份输入，sklearn 与 ONNX 输出必须一致。
    # 弹出可能缓存的同路径旧会话（同进程重复导出时）
    ml_runtime._sessions.pop(str(out), None)
    if verify_rows is not None and len(verify_rows) > 0:
        _verify(spec, result, out, meta, verify_rows)

    logger.info("导出完成: %s (%d bytes)", out.name, out.stat().st_size)
    return str(out), str(meta_path)


def _verify(spec, result: TrainResult, onnx_path: Path, meta: dict,
            verify_rows: np.ndarray) -> None:
    """sklearn 原始模型 vs 导出 ONNX 的预测 diff 校验（含 scaler 路径）。"""
    rows = verify_rows[: min(32, len(verify_rows))].astype(np.float32)

    runtime_spec = ModelSpec(
        name=spec.name, artifact=str(onnx_path),
        features=list(spec.features), raw_features=list(spec.raw_features),
        state_features=list(spec.state_features),
        post_transform="none",
        scaler_mean=meta["scaler_mean"], scaler_std=meta["scaler_std"],
    )
    onnx_out = ml_runtime._run_batch(runtime_spec, rows)

    x = (rows - np.asarray(meta["scaler_mean"], dtype=np.float32)) / np.where(
        np.asarray(meta["scaler_std"], dtype=np.float32) > 1e-10,
        np.asarray(meta["scaler_std"], dtype=np.float32), 1.0,
    )
    if spec.state_features:
        sk_out = result.model.predict_proba(x)[:, 1]
    else:
        sk_out = result.model.predict(x)
    diff = float(np.max(np.abs(sk_out - onnx_out)))
    logger.info("ONNX 一致性校验: max diff=%.2e", diff)
    if diff > 1e-4:
        raise RuntimeError(
            f"ONNX 输出与 sklearn 不一致 (max diff={diff:.2e})——导出失败"
        )
