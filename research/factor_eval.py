"""因子评估工具 — IC 分析 / 分层回测 / 评估编排。

指标计算是无状态纯函数（输入 pd.Series，输出标量/Series）；
run_eval 是因子评估主流程编排（backend 可注入，便于 MockDataBackend 测试），
scripts/factor_eval.py 只留 argparse 薄壳。
"""

import sys
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

from btcore.factors import ops
from btcore.factors import plan as factor_plan
from btcore.factors.library import (
    compute_breadth,
    compute_factors,
    load_library,
    resolve_closure,
)
from btcore.factors.plan import derive_fields, ensure_pseudo_columns
from btcore.filters import resolve_index_snapshots
from btcore.ml import runtime as ml_runtime
from btcore.ml.dataset import apply_pit_membership
from btcore.ml.spec import ModelSpec

# 常用指数代码到简称的映射（便于 --universe 参数）
_UNIVERSE_MAP = {
    "CSI300": "000300.SH",
    "CSI500": "000905.SH",
    "CSI1000": "000852.SH",
    "000300.SH": "000300.SH",
    "000905.SH": "000905.SH",
    "000852.SH": "000852.SH",
}


def _fmt_number(x):
    """格式化数值：四位小数，NaN/None 显示为 '-' 。"""
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    return f"{x:.4f}"


def _print_section(title: str) -> None:
    print(f"\n{'=' * 60}")
    print(f"  {title}")
    print(f"{'=' * 60}")


def run_eval(
    backend,
    factor_names: list[str],
    start: str,
    end: str,
    model_path: str | None = None,
    universe: str | None = None,
    forward: int = 5,
    decay: str | None = None,
    n_quantiles: int = 5,
    benchmark: str | None = None,
) -> int:
    """因子评估主流程（backend 可注入，便于用 MockDataBackend 测试）。"""
    # ML 模型：spec 解析（fail-fast），特征并入因子计算与请求列
    model_spec = None
    if model_path:
        try:
            model_spec = ModelSpec.from_dict(
                Path(model_path).stem, {"artifact": model_path}, "",
            )
        except ValueError as exc:
            # 2026-08 审计：此前裸 traceback（meta 与特征维度不一致等）
            print(f"错误：模型加载失败: {exc}", file=sys.stderr)
            return 1
        if model_spec.scope != "panel":
            print("错误：--model 只支持 panel scope 模型（holding scope 无物化列）",
                  file=sys.stderr)
            return 1

    factor_names = list(dict.fromkeys(factor_names))
    if model_spec is not None:
        factor_names = list(dict.fromkeys(model_spec.features + factor_names))
    if not factor_names and model_spec is None:
        print("错误：至少需要一个因子名称", file=sys.stderr)
        return 1

    # 加载因子库
    library = load_library()
    for name in factor_names:
        if name not in library:
            print(f"错误：未知因子 '{name}'，可用: {sorted(library)}", file=sys.stderr)
            return 1

    print(f"因子: {', '.join(factor_names)}")
    if decay:
        horizons = [int(h.strip()) for h in decay.split(",") if h.strip()]
        if not horizons:
            print("错误：--decay 需要至少一个天数", file=sys.stderr)
            return 1
        print(f"区间: {start} ~ {end}  |  前瞻: {horizons} (衰减模式)  |  "
              f"分档: {n_quantiles}")
    else:
        print(f"区间: {start} ~ {end}  |  前瞻: {forward}d  |  "
              f"分档: {n_quantiles}")

    # 确定股票池
    pit_members = None
    if universe:
        idx_code = _UNIVERSE_MAP.get(universe.upper(), universe)
        print(f"股池: {idx_code}")

        # 区间成分快照（前溯 INDEX_LOOKBACK_DAYS，与引擎同口径）；
        # 并集作为候选股票池（PIT 过滤在因子面板上再做）
        idx_map = resolve_index_snapshots(backend, [idx_code], start, end)
        if not idx_map:
            print(f"警告：{idx_code} 在区间内无成分数据", file=sys.stderr)
        candidate_symbols = sorted(set().union(*idx_map.values()))
        if not candidate_symbols:
            print("错误：未找到任何成分股", file=sys.stderr)
            return 1
        symbols: list[str] | None = candidate_symbols
        pit_members = idx_map
    else:
        symbols = None  # 全市场
        print("股池: 全市场")

    # 列/伪列需求统一走 build_factor_plan 推导（与引擎 preload 同源）：
    # 引用 log_mktcap 自动补 total_mv，伪列不向 backend 请求；
    # model_spec.features 已在上面并入 factor_names
    nodes = resolve_closure(factor_names, library)
    fplan = factor_plan.build_factor_plan(nodes, factor_names)
    raw_cols = set(fplan["main_columns"])
    if model_spec is not None:
        raw_cols |= set(model_spec.raw_features)
    # 补齐 derive_fields / 前瞻收益所需的列
    raw_cols |= {"open", "high", "low", "close", "adj_factor", "pre_close"}
    request_columns = factor_plan.expand_columns(raw_cols)

    # warmup 前伸与引擎 preload 同源（engine.py: preload_start =
    # 首日 - fplan main_days 日历天），滚动因子在窗口头部才有值
    warmup_start = (
        pd.Timestamp(start) - pd.Timedelta(days=fplan["main_days"])
    ).strftime("%Y%m%d")
    print(f"请求列: {len(request_columns)} 列  |  warmup: {warmup_start} ~ {start}")

    # 查询行情面板
    bars_df = backend.query_bars(
        symbols, warmup_start, end, columns=request_columns,
    )

    if bars_df.empty:
        print("错误：区间内无行情数据", file=sys.stderr)
        return 1

    # 补齐后复权价等派生列
    derive_fields(bars_df)

    # 附着伪列（industry / log_mktcap / idx_ret），needs 由 fplan 推导（引擎同源）
    ensure_pseudo_columns(
        bars_df, fplan["needs"], "main",
        backend=backend, benchmark=benchmark,
    )

    # 坍缩因子（市场广度）：全市场流式口径（引擎同源 compute_breadth），
    # 不在候选池面板上做截面均值——那会得到"指数池内占比"的错误数字
    collapse_names = [
        n for n in factor_names if ops.collapse_kind(library[n]["expr"])
    ]
    panel_names = [n for n in factor_names if n not in collapse_names]
    # closure 内被引用但非顶层求值的坍缩因子：面板语义错误，fail-fast
    for node_name, node_spec in nodes.items():
        if node_name in factor_names:
            continue
        if ops.collapse_kind(node_spec["expr"]):
            print(
                f"错误: 因子 {node_name} 是坍缩因子（市场广度），但被其他因子"
                "经表达式引用——factor_eval 不支持嵌套坍缩，请将其作为"
                "独立顶层因子评估", file=sys.stderr,
            )
            return 1

    # 计算因子值（保形因子走面板；坍缩因子走全市场流式）
    print(f"计算因子: {', '.join(factor_names)} ...")
    if panel_names:
        factor_df = compute_factors(panel_names, bars_df, library)
    else:
        factor_df = pd.DataFrame(index=bars_df.index)
    for name in collapse_names:
        daily = compute_breadth(name, backend, library, start, end,
                                benchmark=benchmark)
        if daily.empty:
            print(f"警告: 坍缩因子 {name} 无广度数据", file=sys.stderr)
        factor_df[name] = factor_df.index.get_level_values("trade_date").map(daily)
        print(f"  坍缩因子 {name}: 全市场广度口径（compute_breadth，引擎同源）")
    print(f"  有效截面: {len(factor_df)} 行  |  "
          f"日期数: {factor_df.index.get_level_values('trade_date').nunique()}")

    # ML 模型：分数物化为 ml_<name> 列，后续与因子同口径评估
    eval_names = list(factor_names)
    if model_spec is not None:
        eval_df = bars_df.copy()
        for c in model_spec.features:
            eval_df[c] = factor_df[c]
        ml_runtime.materialize_predictions(eval_df, [model_spec])
        factor_df[model_spec.column] = eval_df[model_spec.column]
        eval_names.append(model_spec.column)
        print(f"模型分数: {model_spec.column} "
              f"(post_transform={model_spec.post_transform})")

    # --universe：point-in-time 成分过滤（与 ml_train 训练域一致，
    # 引擎逐日计算域）；快照缺失的日期行被过滤
    if pit_members:
        factor_df = apply_pit_membership(factor_df, pit_members)
        print(f"PIT 成分过滤后: {len(factor_df)} 行")

    # 裁剪回用户窗口（warmup 行只用于因子取值，不进入 IC 统计）
    dts = factor_df.index.get_level_values("trade_date")
    factor_df = factor_df.loc[(dts >= start) & (dts <= end)]
    if factor_df.empty:
        print("错误: 窗口内无有效因子数据", file=sys.stderr)
        return 1

    # 计算前瞻收益（单期，供分层回测使用）
    close_hfq = bars_df["close_hfq"]
    fwd_ret_layered = close_hfq.groupby("symbol").pct_change(
        periods=forward
    ).shift(-forward)
    fwd_ret_layered.name = "fwd_ret"

    # 坍缩因子截面恒值 → corr 标准差为 0 的 RuntimeWarning，抑制噪音
    warnings.filterwarnings("ignore", category=RuntimeWarning)

    if decay:
        # ── IC 衰减模式 ──
        _print_section(f"IC 衰减曲线（前瞻: {horizons}）")
        for name in eval_names:
            factor_vals = factor_df[name]
            decay_df = calc_ic_decay(factor_vals, close_hfq, horizons)
            print(f"\n  {name}:")

            # 表头
            header = (f"  {'前瞻':>6s}  {'IC':>8s}  {'IC IR':>7s}  {'IC Win':>7s}  "
                      f"{'RankIC':>8s}  {'RankIR':>7s}  {'Win':>7s}  {'n'}")
            print(header)
            print("  " + "-" * (len(header) - 2))

            for h in horizons:
                row = decay_df.loc[h]
                n_days = int(row["n_days"])
                print(
                    f"  {h:>6d}  "
                    f"{_fmt_number(row['ic_mean']):>8s}  "
                    f"{_fmt_number(row['ic_ir']):>7s}  "
                    f"{_fmt_number(row['ic_win']):>7s}  "
                    f"{_fmt_number(row['rank_ic_mean']):>8s}  "
                    f"{_fmt_number(row['rank_ic_ir']):>7s}  "
                    f"{_fmt_number(row['rank_ic_win']):>7s}  "
                    f"({n_days}d)"
                )

            # 衰减趋势总结
            first_ric = decay_df.loc[horizons[0], "rank_ic_mean"]
            last_ric = decay_df.loc[horizons[-1], "rank_ic_mean"]
            if not pd.isna(first_ric) and not pd.isna(last_ric):
                trend = "↘ 衰减" if abs(last_ric) < abs(first_ric) else "↗ 增强"
                print(f"    趋势: RankIC {first_ric:.4f}({horizons[0]}d)"
                      f" → {last_ric:.4f}({horizons[-1]}d)  {trend}")
    else:
        # ── 1. IC 分析（单期模式）──
        fwd_ret = fwd_ret_layered
        _print_section("IC 汇总")
        ic_results = {}
        for name in eval_names:
            factor_vals = factor_df[name]
            ic, ric = calc_ic(factor_vals, fwd_ret)
            pearson = summarize_ic(ic)
            spearman = summarize_ic(ric)
            ic_results[name] = {
                "pearson_mean": pearson["ic_mean"],
                "pearson_ir": pearson["icir"],
                "pearson_win": pearson["ic_positive_ratio"],
                "spearman_mean": spearman["ic_mean"],
                "spearman_ir": spearman["icir"],
                "spearman_win": spearman["ic_positive_ratio"],
                "n_days": pearson["n_days"],
            }
            print(
                f"  {name:<20s}  "
                f"IC={_fmt_number(pearson['ic_mean']):>8s}  "
                f"IR={_fmt_number(pearson['icir']):>7s}  "
                f"Win={_fmt_number(pearson['ic_positive_ratio']):>7s}  "
                f"|  RankIC={_fmt_number(spearman['ic_mean']):>8s}  "
                f"RankIR={_fmt_number(spearman['icir']):>7s}  "
                f"({pearson['n_days']}d)"
            )

    # ── 分层回测（单期，forward 始终适用）──
    _print_section(f"分层回测（{n_quantiles} 档，{forward}d 前瞻）")
    for name in eval_names:
        layers = calc_layered_returns(
            factor_df[name], fwd_ret_layered, n_quantiles=n_quantiles,
        )
        if not layers:
            print(f"  {name}: 无数据")
            continue
        print(f"  {name}:")
        for q in sorted(layers):
            cum = layers[q]
            final = cum.iloc[-1] if len(cum) > 0 else float("nan")
            print(f"    Q{q} (第{q}档): 累计={_fmt_number(final - 1):>8s}  "
                  f"({len(cum)}d)")
        # 多空收益（最高档 - 最低档）
        q_max = max(layers)
        q_min = min(layers)
        if q_max != q_min and len(layers[q_max]) > 0 and len(layers[q_min]) > 0:
            long_short = layers[q_max].iloc[-1] - layers[q_min].iloc[-1]
            print(f"    多空 (Q{q_max}-Q{q_min}): {_fmt_number(long_short):>8s}")

    # ── 3. 因子相关性 ──
    _print_section("因子相关性矩阵（截面 Pearson 均值）")
    if len(eval_names) >= 2:
        corr_mat = calc_factor_corr(factor_df)
        if not corr_mat.empty:
            # 打印格式化矩阵
            header = "          " + "  ".join(f"{n:>8s}" for n in corr_mat.columns)
            print(header)
            for row_name in corr_mat.index:
                vals = "  ".join(
                    _fmt_number(corr_mat.loc[row_name, col])
                    for col in corr_mat.columns
                )
                print(f"  {row_name:<8s}  {vals}")
        else:
            print("  数据不足，无法计算")
    else:
        print("  需要 ≥ 2 个因子才能计算相关性矩阵")

    print()
    return 0


def calc_ic(factor_values: pd.Series, forward_returns: pd.Series,
            date_col: str = "trade_date") -> tuple[pd.Series, pd.Series]:
    """计算每日截面 IC 和 Rank IC。

    Args:
        factor_values: MultiIndex (date, symbol) 的因子值。
        forward_returns: 同结构的未来收益。
        date_col: 日期索引名。

    Returns:
        (ic_series, rank_ic_series): 每日 Pearson IC 和 Spearman Rank IC。
    """
    df = pd.DataFrame({"factor": factor_values, "fwd_ret": forward_returns}).dropna()
    if df.empty:
        return pd.Series(dtype=float), pd.Series(dtype=float)

    def _corr(g, rank: bool):
        f = g["factor"]
        r = g["fwd_ret"]
        if rank:
            f = f.rank()
            r = r.rank()
        if len(f) < 3:
            return np.nan
        return float(f.corr(r))

    grouped = df.groupby(level=date_col, group_keys=False)
    ic = grouped.apply(_corr, rank=False)
    ric = grouped.apply(_corr, rank=True)
    return ic, ric


def calc_layered_returns(
    factor_values: pd.Series,
    forward_returns: pd.Series,
    n_quantiles: int = 5,
    date_col: str = "trade_date",
) -> dict[int, pd.Series]:
    """分层回测：按因子值分 N 档，每档等权持有，输出累计收益曲线。

    Args:
        factor_values: (date, symbol) 因子值。
        forward_returns: (date, symbol) 未来收益。
        n_quantiles: 分档数（默认 5 档）。
        date_col: 日期索引名。

    Returns:
        {q: cumulative_return_series}，q=1 为因子值最低档，q=N 为最高档
        （pd.qcut labels=False 的 0 档是最低值区间，+1 后 q=1 即最低档）。
    """
    df = pd.DataFrame({"factor": factor_values, "fwd_ret": forward_returns}).dropna()
    if df.empty:
        return {}

    # 每日按因子值分档
    df["quantile"] = df.groupby(level=date_col, group_keys=False)["factor"].transform(
        lambda x: pd.qcut(x, n_quantiles, labels=False, duplicates="drop") + 1
    )

    # 每档每日等权平均收益
    daily_qret = df.groupby([date_col, "quantile"])["fwd_ret"].mean().unstack("quantile")

    # 累计收益
    result = {}
    for q in sorted(daily_qret.columns):
        cum = (1 + daily_qret[q].dropna()).cumprod()
        result[int(q)] = cum
    return result


def summarize_ic(ic_series: pd.Series) -> dict:
    """IC 汇总统计。

    Returns:
        {ic_mean, ic_std, icir, ic_positive_ratio, n_days}
    """
    ic = ic_series.dropna()
    if len(ic) == 0:
        return {"ic_mean": 0.0, "ic_std": 0.0, "icir": 0.0,
                "ic_positive_ratio": 0.0, "n_days": 0}

    mean_ic = float(ic.mean())
    std_ic = float(ic.std(ddof=1))
    return {
        "ic_mean": mean_ic,
        "ic_std": std_ic,
        "icir": mean_ic / std_ic if std_ic > 0 else 0.0,
        "ic_positive_ratio": float((ic > 0).mean()),
        "n_days": len(ic),
    }


def calc_factor_corr(
    factor_df: pd.DataFrame,
    date_col: str = "trade_date",
) -> pd.DataFrame:
    """计算因子截面相关性矩阵（按日求 Pearson corr，再取均值）。

    Args:
        factor_df: MultiIndex (date, symbol) 宽表，每列一个因子值。
        date_col: 日期索引名。

    Returns:
        因子间平均相关性矩阵 (DataFrame, index/columns = 因子名)。
        样本不足 3 个的日期跳过；若全无有效日则返回 NaN 矩阵。
    """
    if factor_df.empty or factor_df.shape[1] < 2:
        return pd.DataFrame()

    corr_mats = []
    for dt, g in factor_df.groupby(level=date_col, group_keys=False):
        g_clean = g.dropna(axis=1, how="all").dropna(axis=0)
        if g_clean.shape[0] < 3 or g_clean.shape[1] < 2:
            continue
        corr_mats.append(g_clean.corr())

    if not corr_mats:
        columns = list(factor_df.columns)
        return pd.DataFrame(np.nan, index=columns, columns=columns)

    avg_corr = sum(corr_mats) / len(corr_mats)
    return avg_corr


def calc_ic_decay(
    factor_values: pd.Series,
    close_hfq: pd.Series,
    horizons: list[int],
    date_col: str = "trade_date",
) -> pd.DataFrame:
    """多前瞻期 IC 衰减汇总表。

    对每个 horizon 计算 forward returns 并汇总 IC / Rank IC 统计量，
    输出一张 horizon × 指标 的表，用于判断因子 alpha 的衰减速度。

    Args:
        factor_values: MultiIndex (date, symbol) 的因子值。
        close_hfq: 同结构的后复权收盘价。
        horizons: 前瞻天数列表，如 [1, 3, 5, 10, 20]。
        date_col: 日期索引名。

    Returns:
        DataFrame，索引为 horizon，列为：
          ic_mean, ic_ir, ic_win, rank_ic_mean, rank_ic_ir, rank_ic_win, n_days
    """
    rows = []
    for h in horizons:
        fwd_ret = close_hfq.groupby("symbol").pct_change(h).shift(-h)
        ic, ric = calc_ic(factor_values, fwd_ret, date_col=date_col)
        pearson = summarize_ic(ic)
        spearman = summarize_ic(ric)
        rows.append({
            "horizon": h,
            "ic_mean": pearson["ic_mean"],
            "ic_ir": pearson["icir"],
            "ic_win": pearson["ic_positive_ratio"],
            "rank_ic_mean": spearman["ic_mean"],
            "rank_ic_ir": spearman["icir"],
            "rank_ic_win": spearman["ic_positive_ratio"],
            "n_days": pearson["n_days"],
        })
    return pd.DataFrame(rows).set_index("horizon")
