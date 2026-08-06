"""ML 标准化公共实现 — 训练 / 推理 / 导出校验三侧共用同一数学。

语义：缺失感知标准化（(x - mean) / std，std 极小值以 1.0 兜底）后，
NaN 与 ±inf 全部填 0 —— 标准化空间的 0 = 训练段均值，缺失/发散被解释
为中性"平均水准"（±inf 标准化后仍为 ±inf，由 nan_to_num 一并归 0）。

训练侧（trainer._scale）、推理侧（runtime._apply_scaler/_run_batch）、
导出校验侧（export._verify）必须调用同一实现，任何一侧手写第二份都会
造成口径漂移（CONS-07 / DUP-07）。
"""

import numpy as np


def standardize(arr: np.ndarray, mean, std) -> np.ndarray:
    """标准化 + 缺失/发散填 0。

    mean/std 的 dtype 由调用方保证与 arr 匹配（训练侧 float64、
    推理/校验侧 float32），本函数不做强制转换以免改变既有数值路径。
    """
    return np.nan_to_num(
        (arr - mean) / np.where(std > 1e-10, std, 1.0),
        nan=0.0, posinf=0.0, neginf=0.0,
    )
