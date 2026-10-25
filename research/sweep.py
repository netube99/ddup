"""参数扫描工具函数 — 参数空间展开与嵌套键写入（可 import 库）。

scripts/sweep.py 是薄壳 runner；被测逻辑在本模块。
"""

import itertools


def nested_set(d, key_path, value):
    """按 '.' 分割的路径设置嵌套值，支持列表整数下标（如 factor_specs.0.weight）。"""
    keys = key_path.split(".")
    for k in keys[:-1]:
        if isinstance(d, list):
            d = d[int(k)]
        else:
            d = d.setdefault(k, {})
    if isinstance(d, list):
        d[int(keys[-1])] = value
    else:
        d[keys[-1]] = value


def expand_params(params_def):
    """展开参数空间为笛卡尔积，返回 (param_label, param_dict) 列表。

    params 为空是配置错误：itertools.product() 会产出 1 个空标签组合，
    把 base 配置当扫描结果写库（TOOL-08），此处 fail-fast。
    候选值必须是列表：标量无法展开组合；字符串会被 itertools 按单字符
    展开（文档要求候选值必须为列表），同样在此 fail-fast（F-CLI-03）。
    """
    if not params_def:
        raise ValueError(
            "params 为空：没有可扫描的参数组合（至少需要一个参数路径）"
        )
    for key, candidates in params_def.items():
        if not isinstance(candidates, (list, tuple)):
            raise ValueError(
                f"参数 {key} 的候选值必须是列表，实际 {candidates!r}："
                "标量无法展开组合，字符串会被按单字符展开（写法如 [5, 10]）"
            )
    keys = list(params_def.keys())
    values = [params_def[k] for k in keys]
    results = []
    for combo in itertools.product(*values):
        param_dict = dict(zip(keys, combo))
        label_parts = []
        for k, v in param_dict.items():
            short_k = k.split(".")[-1]  # 取路径最后一段作为简称
            if isinstance(v, float):
                label_parts.append(f"{short_k}={v:.2f}")
            else:
                label_parts.append(f"{short_k}={v}")
        label = ", ".join(label_parts)
        results.append((label, param_dict))
    return results
