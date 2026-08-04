"""训练面板构建 — 与引擎 preload 同一物化路径。

逐行复刻 Engine.run() 的数据准备序列（build_factor_plan → 两路供给 →
derive_fields → 伪列附着 → materialize → 物化验证），全部调用
btcore.factors.plan 的同一组函数——训练面板与回测面板逐列一致，
不存在第二条会漂移的物化管线。

纯函数模块：backend 以参数传入（鸭子类型），不 import adapters。
"""

import logging

import numpy as np
import pandas as pd

from btcore.factors import plan as factor_plan
from btcore.factors.library import resolve_closure
from btcore.ml.spec import ModelSpec

logger = logging.getLogger(__name__)


def build_panel(
    backend,
    symbols: list[str] | None,
    start: str,
    end: str,
    spec: ModelSpec,
    library: dict,
    benchmark: str | None = None,
) -> pd.DataFrame:
    """构建模型的训练特征面板（含物化因子列 + raw 列），裁到 [start, end]。

    Args:
        backend: DataBackend 实例（query_bars / get_benchmark_bars 等鸭子类型）。
        symbols: 股票列表；None = 全市场。
        start / end: 训练区间（warmup 前伸由因子计划自动推导）。
        spec: ModelSpec（特征契约来源）。
        library: 因子库 dict。
        benchmark: 基准代码（因子引用 idx_ret 时必需，口径同引擎）。
    """
    factor_names = list(spec.features)
    if not factor_names and not spec.raw_features:
        raise ValueError(f"模型 {spec.name} 无面板特征")

    columns = set(factor_plan.REQUIRED_BAR_COLUMNS) | set(spec.raw_features)
    fplan = None
    nodes = None
    if factor_names:
        nodes = resolve_closure(factor_names, library)
        fplan = factor_plan.build_factor_plan(nodes, factor_names)
        columns |= fplan["main_columns"]

    warmup_days = fplan["main_days"] if fplan else factor_plan.DEFAULT_WARMUP_DAYS
    load_start = (pd.Timestamp(start) - pd.Timedelta(days=warmup_days)).strftime("%Y%m%d")
    request_columns = factor_plan.expand_columns(columns)

    logger.info(
        "训练面板加载: %s ~ %s, %d 列, warmup=%dd",
        load_start, end, len(request_columns), warmup_days,
    )
    bars_df = backend.query_bars(symbols, load_start, end, columns=request_columns)
    if bars_df is None or len(bars_df) == 0:
        raise RuntimeError("backend.query_bars 返回空数据")
    bars_df.sort_index(inplace=True)
    factor_plan.validate_required_columns(bars_df)
    factor_plan.derive_fields(bars_df)

    if fplan:
        breadth_df = None
        if fplan["needs"]["market"]:
            breadth_start = (
                pd.Timestamp(start) - pd.Timedelta(days=fplan["breadth_days"])
            ).strftime("%Y%m%d")
            breadth_df = backend.query_bars(
                None, breadth_start, end,
                columns=factor_plan.expand_columns(fplan["breadth_columns"]),
            )
            breadth_df.sort_index(inplace=True)
            factor_plan.derive_fields(breadth_df)
            factor_plan.ensure_pseudo_columns(
                breadth_df, fplan["needs"], "breadth",
                backend=backend, benchmark=benchmark,
            )
        factor_plan.ensure_pseudo_columns(
            bars_df, fplan["needs"], "main",
            backend=backend, benchmark=benchmark,
        )
        factor_plan.materialize(bars_df, breadth_df, fplan)
        issues = factor_plan.validate_materialization(bars_df, fplan)
        for issue in issues:
            getattr(logger, issue["level"])("[因子验证] %s", issue["message"])

    # 裁掉 warmup，返回训练区间
    dates = bars_df.index.get_level_values("trade_date")
    result = bars_df.loc[(dates >= start) & (dates <= end)]
    # ±inf（因子表达式除零等）归一为 NaN，并入既有缺失体系：scaler 缺失感知
    # 拟合，推理侧 nan_to_num 把缺失/发散填 0（= 训练段均值），两侧同口径
    result = result.replace([np.inf, -np.inf], np.nan)
    logger.info(
        "训练面板: %d 行, %d 只, %s ~ %s",
        len(result), result.index.get_level_values("symbol").nunique(), start, end,
    )
    return result


def apply_pit_membership(
    panel: pd.DataFrame, members_by_date: dict | None,
) -> pd.DataFrame:
    """按 point-in-time 成分过滤训练面板（训练域 = 引擎逐日计算域）。

    members_by_date: get_index_members 的 {快照日期: {symbol, ...}}；每行取
    ≤ 当日最近的成分快照（与 filters._attach_index_universe 同口径）。
    未配置 index_universe 时返回原面板。
    """
    if not members_by_date:
        return panel
    members_by_date = {str(d): set(v) for d, v in members_by_date.items()}
    snap_dates = np.array(sorted(members_by_date), dtype=object)
    dts = panel.index.get_level_values("trade_date").to_numpy(dtype=object)
    syms = panel.index.get_level_values("symbol")
    snap_idx = snap_dates.searchsorted(dts, side="right") - 1
    mask = np.zeros(len(panel), dtype=bool)
    for k, d in enumerate(snap_dates):
        rows_k = np.flatnonzero(snap_idx == k)
        if rows_k.size:
            mask[rows_k] = syms.take(rows_k).isin(members_by_date[d])
    panel = panel[mask]
    if panel.empty:
        raise RuntimeError(
            "PIT 训练域过滤后为空——index_universe 快照与训练窗口无交集"
        )
    return panel
