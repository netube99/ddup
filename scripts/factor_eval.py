"""因子评估 CLI — IC 分析 / 分层回测 / 因子相关性矩阵。

用法:
    python scripts/factor_eval.py mom20,vol_z,ep_z \
        --start 20240101 --end 20240630 [--universe CSI300] [--forward 5] \
        [--n-quantiles 5]

IC 衰减模式（多前瞻期）:
    python scripts/factor_eval.py cci_z,turnover_z \
        --start 20240101 --end 20240630 --decay 1,3,5,10,20

行情数据库由 adapters/tushare.py 的 _DEFAULT_DB_PATH 决定。
"""

import argparse
import sys

import pandas as pd

from adapters.tushare import TushareBackend
from btcore.engine import _ensure_derived_fields, ensure_pseudo_columns
from btcore.factors import plan as factor_plan
from btcore.factors.library import compute_factors, load_library, spec_names
from research.factor_eval import (
    calc_factor_corr,
    calc_ic,
    calc_ic_decay,
    calc_layered_returns,
    summarize_ic,
)

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="因子评估 — IC / 分层回测 / 相关性矩阵",
    )
    parser.add_argument(
        "factors", help="逗号分隔的因子名称（来自 factors/library.yaml）",
    )
    parser.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument(
        "--universe", default=None,
        help="指数代码或简称（CSI300/CSI500/CSI1000），默认全市场",
    )
    parser.add_argument(
        "--forward", type=int, default=5,
        help="前瞻收益天数（默认 5，即 1 周）",
    )
    parser.add_argument(
        "--decay", type=str, default=None,
        help="多前瞻期 IC 衰减模式（逗号分隔天数，如 1,3,5,10,20）",
    )
    parser.add_argument(
        "--n-quantiles", type=int, default=5,
        help="分层回测档数（默认 5）",
    )
    args = parser.parse_args()

    # --decay 与 --forward 互斥
    if args.decay and args.forward != 5:
        print("错误：--decay 与 --forward 不能同时指定", file=sys.stderr)
        return 1

    factor_names = [n.strip() for n in args.factors.split(",") if n.strip()]
    if not factor_names:
        print("错误：至少需要一个因子名称", file=sys.stderr)
        return 1

    # 加载因子库
    library = load_library()
    for name in factor_names:
        if name not in library:
            print(f"错误：未知因子 '{name}'，可用: {sorted(library)}", file=sys.stderr)
            return 1

    print(f"因子: {', '.join(factor_names)}")
    if args.decay:
        horizons = [int(h.strip()) for h in args.decay.split(",") if h.strip()]
        if not horizons:
            print("错误：--decay 需要至少一个天数", file=sys.stderr)
            return 1
        print(f"区间: {args.start} ~ {args.end}  |  前瞻: {horizons} (衰减模式)  |  "
              f"分档: {args.n_quantiles}")
    else:
        print(f"区间: {args.start} ~ {args.end}  |  前瞻: {args.forward}d  |  "
              f"分档: {args.n_quantiles}")

    # 连接后端
    backend = TushareBackend()

    # 确定股票池
    if args.universe:
        idx_code = _UNIVERSE_MAP.get(args.universe.upper(), args.universe)
        print(f"股池: {idx_code}")

        # 取区间内最近一期成分快照（向前回溯一段）
        lookback_start = (
            pd.Timestamp(args.start) - pd.Timedelta(days=45)
        ).strftime("%Y%m%d")
        idx_map = backend.get_index_members(
            [idx_code], lookback_start, args.end,
        )
        if not idx_map:
            print(f"警告：{idx_code} 在区间内无成分数据", file=sys.stderr)

        # 取区间成分的并集作为候选股票池
        dates_in_range = [
            d for d in sorted(idx_map) if lookback_start <= d <= args.end
        ]
        candidate_symbols: list[str] = []
        seen = set()
        for d in dates_in_range:
            for s in idx_map.get(d, set()):
                if s not in seen:
                    seen.add(s)
                    candidate_symbols.append(s)
        if not candidate_symbols:
            print("错误：未找到任何成分股", file=sys.stderr)
            backend.close()
            return 1
        symbols: list[str] | None = candidate_symbols
    else:
        symbols = None  # 全市场
        print("股池: 全市场")

    # 确定需要请求的列：因子依赖的基础列 + hfq 派生所需列
    raw_cols: set[str] = set()
    for name in factor_names:
        spec = library[name]
        cols, _ = spec_names(spec, set(library))
        raw_cols |= cols
    # 补齐 _ensure_derived_fields 所需的列
    raw_cols |= {"open", "high", "low", "close", "adj_factor", "pre_close"}
    # 伪列（industry / log_mktcap / idx_ret）不向 backend 请求，后续由引擎附着
    request_columns = factor_plan.expand_columns(raw_cols)
    print(f"请求列: {len(request_columns)} 列")

    # 查询行情面板
    bars_df = backend.query_bars(
        symbols, args.start, args.end, columns=request_columns,
    )

    if bars_df.empty:
        backend.close()
        print("错误：区间内无行情数据", file=sys.stderr)
        return 1

    # 补齐后复权价等派生列
    _ensure_derived_fields(bars_df)

    # 附着伪列（industry / log_mktcap / idx_ret），与引擎 preload 口径一致
    pseudo_needs = {
        "industry_main": "industry" in raw_cols,
        "mktcap_main": "log_mktcap" in raw_cols,
        "index": "idx_ret" in raw_cols,
    }
    if any(pseudo_needs.values()):
        ensure_pseudo_columns(bars_df, pseudo_needs, "main", backend=backend)

    backend.close()

    # 计算因子值
    print(f"计算因子: {', '.join(factor_names)} ...")
    factor_df = compute_factors(factor_names, bars_df, library)
    print(f"  有效截面: {len(factor_df)} 行  |  "
          f"日期数: {factor_df.index.get_level_values('trade_date').nunique()}")

    # 计算前瞻收益（单期，供分层回测使用）
    close_hfq = bars_df["close_hfq"]
    fwd_ret_layered = close_hfq.groupby("symbol").pct_change(
        periods=args.forward
    ).shift(-args.forward)
    fwd_ret_layered.name = "fwd_ret"

    if args.decay:
        # ── IC 衰减模式 ──
        _print_section(f"IC 衰减曲线（前瞻: {horizons}）")
        for name in factor_names:
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
        for name in factor_names:
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
    _print_section(f"分层回测（{args.n_quantiles} 档，{args.forward}d 前瞻）")
    for name in factor_names:
        layers = calc_layered_returns(
            factor_df[name], fwd_ret_layered, n_quantiles=args.n_quantiles,
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
    if len(factor_names) >= 2:
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


if __name__ == "__main__":
    sys.exit(main())
