"""主板池因子评估 — 板块前缀过滤 + IC/分层（研究用脚本，非正式 CLI 契约）。

用法:
    python scripts/board_factor_eval.py dv_z,vol_z,ep_z \
        --start 20240101 --end 20250630 [--pool main] [--forward 5] \
        [--decay 5,10,20] [--exec-price close|next-open]

池（--pool）:
    all       全市场（对照）
    main      纯主板：剔除 300/301/688/BJ（与引擎 _get_board 同口径）
    main500   主板 + 当日总市值前 500（log_mktcap 截面 top500，PIT 每日重排）
    main1000  主板 + 当日总市值前 1000

口径与 factor_eval 同源：因子物化 warmup 前伸、显式 groupby shift 前瞻收益、
next-open = T+1 开盘买（引擎同款执行时序）。
"""

import argparse
import sys
import warnings

import pandas as pd

from btcore.factors import ops
from btcore.factors import plan as factor_plan
from btcore.factors.library import compute_factors, load_library, resolve_closure
from btcore.factors.plan import derive_fields, ensure_pseudo_columns
from research import cli_common
from research.factor_eval import (
    calc_ic,
    calc_ic_decay,
    calc_layered_returns,
    summarize_ic,
)

_POOLS = ("all", "main", "main500", "main1000")


def _is_main(symbol: str) -> bool:
    """引擎 _get_board 同口径：非 BJ/688/300/301 即主板。"""
    if symbol.endswith(".BJ"):
        return False
    code = symbol.split(".")[0]
    return not (code.startswith("688") or code.startswith("300")
                or code.startswith("301"))


def _pool_mask(index: pd.Index, pool: str, bars_df: pd.DataFrame) -> pd.Series:
    """返回与 factor 面板同序的布尔掩码。"""
    symbols = index.get_level_values("symbol")
    if pool == "all":
        return pd.Series(True, index=index)
    if pool == "main":
        return pd.Series([_is_main(s) for s in symbols], index=index)
    # main500 / main1000：PIT 每日按 log_mktcap 截面排名
    n = int(pool.replace("main", ""))
    rank = bars_df["log_mktcap"].groupby("trade_date").rank(
        ascending=False, method="first"
    )
    rank = rank.reindex(index)
    return pd.Series(rank <= n, index=index)


def _fmt(x: float) -> str:
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return "-"
    return f"{x:.4f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="主板池因子评估")
    parser.add_argument("factors", help="逗号分隔因子名")
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--pool", default="main", choices=_POOLS)
    parser.add_argument("--forward", type=int, default=5)
    parser.add_argument("--decay", default=None)
    parser.add_argument("--exec-price", default="close",
                        choices=["close", "next-open"])
    args = parser.parse_args()

    factor_names = [n.strip() for n in args.factors.split(",") if n.strip()]
    library = load_library()
    for name in factor_names:
        if name not in library:
            print(f"错误：未知因子 '{name}'", file=sys.stderr)
            return 1

    backend = cli_common.make_provider().backend
    try:
        nodes = resolve_closure(factor_names, library)
        fplan = factor_plan.build_factor_plan(nodes, factor_names)
        raw_cols = set(fplan["main_columns"])
        raw_cols |= {"open", "high", "low", "close", "adj_factor", "pre_close"}
        # main500/main1000 池需要 log_mktcap（由 total_mv 派生，伪列不请求）
        raw_cols |= {"total_mv"}
        request_columns = factor_plan.expand_columns(raw_cols)

        warmup_start = (
            pd.Timestamp(args.start) - pd.Timedelta(days=fplan["main_days"])
        ).strftime("%Y%m%d")
        bars_df = backend.query_bars(None, warmup_start, args.end,
                                     columns=request_columns)
        if bars_df.empty:
            print("错误：区间内无行情数据", file=sys.stderr)
            return 1
        derive_fields(bars_df)
        needs = dict(fplan["needs"])
        needs["mktcap_main"] = True
        ensure_pseudo_columns(bars_df, needs, "main", backend=backend)

        # 坍缩因子走全市场广度（与 factor_eval 同源）
        collapse_names = [
            n for n in factor_names if ops.collapse_kind(library[n]["expr"])
        ]
        panel_names = [n for n in factor_names if n not in collapse_names]
        for node_name, node_spec in nodes.items():
            if node_name in factor_names:
                continue
            if ops.collapse_kind(node_spec["expr"]):
                print(f"错误: 因子 {node_name} 是坍缩因子但被其他因子经表达式引用",
                      file=sys.stderr)
                return 1
        factor_df = (
            compute_factors(panel_names, bars_df, library)
            if panel_names
            else pd.DataFrame(index=bars_df.index)
        )
        for name in collapse_names:
            from btcore.factors.library import compute_breadth
            daily = compute_breadth(name, backend, library, args.start, args.end)
            factor_df[name] = factor_df.index.get_level_values(
                "trade_date").map(daily)
            print(f"  坍缩因子 {name}: 全市场广度口径")

        # 前瞻收益（显式 groupby shift，与 factor_eval 同源）
        close_hfq = bars_df["close_hfq"]
        if args.exec_price == "next-open":
            fwd_ret = (
                close_hfq.groupby("symbol").shift(-args.forward)
                / bars_df["open_hfq"].groupby("symbol").shift(-1)
                - 1
            )
        else:
            fwd_ret = (
                close_hfq.groupby("symbol").shift(-args.forward) / close_hfq - 1
            )
        fwd_ret.name = "fwd_ret"

        # 裁剪回用户窗口（warmup 行只用于因子取值，不进入 IC 统计）
        dts = factor_df.index.get_level_values("trade_date")
        keep_rows = (dts >= args.start) & (dts <= args.end)
        factor_df = factor_df.loc[keep_rows]
        fwd_ret = fwd_ret[keep_rows]
        if factor_df.empty:
            print("错误: 窗口内无有效因子数据", file=sys.stderr)
            return 1

        print(f"因子: {', '.join(factor_names)}  |  区间: {args.start}~{args.end}  |  "
              f"前瞻: {args.forward}d  |  口径: {args.exec_price}")
        warnings.filterwarnings("ignore", category=RuntimeWarning)

        # 每个池单独出 IC 汇总（除 --pool 指定池外，其余池只出 RankIR 一行）
        print("\n" + "=" * 60)
        print(f"  各池 RankIC / RankIR 对比（{args.forward}d 前瞻为列）")
        print("=" * 60)
        header = (f"  {'池':<10s}  {'均值股票数':>8s}  " + "  ".join(
            f"{n:<18s}" for n in factor_names))
        print(header)
        print("  " + "-" * len(header))
        for pool in _POOLS:
            mask = _pool_mask(factor_df.index, pool, bars_df)
            if pool == args.pool:
                # 指定池：完整输出走下面主流程
                continue
            n_stocks = mask.groupby(
                factor_df.index.get_level_values("trade_date")).sum()
            parts = [f"{pool:<10s}  {int(n_stocks.median()):>8d}"]
            for name in factor_names:
                vals = factor_df[name][mask]
                fwd = fwd_ret[mask]
                _, ric = calc_ic(vals, fwd)
                s = summarize_ic(ric)
                parts.append(f"{s['ic_mean']:+.4f}/{s['icir']:+.3f}")
            print("  " + "  ".join(parts))

        # 指定池完整评估
        pool = args.pool
        mask = _pool_mask(factor_df.index, pool, bars_df)
        n_stocks = mask.groupby(
            factor_df.index.get_level_values("trade_date")).sum()
        fdf = factor_df[mask]
        fwd = fwd_ret[mask]
        print(f"\n{'=' * 60}")
        print(f"  指定池 {pool}（日均 {int(n_stocks.median())} 只）— IC 汇总")
        print("=" * 60)
        if args.decay:
            horizons = [int(h) for h in args.decay.split(",")]
            for name in factor_names:
                print(f"\n  {name}:")
                ddf = calc_ic_decay(fdf[name], close_hfq, horizons,
                                    open_hfq=bars_df["open_hfq"]
                                    if args.exec_price == "next-open" else None)
                print(f"  {'前瞻':>6s}  {'IC':>8s}  {'IC IR':>7s}  "
                      f"{'RankIC':>8s}  {'RankIR':>7s}  {'Win':>7s}  {'n'}")
                for h in horizons:
                    row = ddf.loc[h]
                    print(f"  {h:>6d}  {_fmt(row['ic_mean']):>8s}  "
                          f"{_fmt(row['ic_ir']):>7s}  "
                          f"{_fmt(row['rank_ic_mean']):>8s}  "
                          f"{_fmt(row['rank_ic_ir']):>7s}  "
                          f"{_fmt(row['rank_ic_win']):>7s}  ({int(row['n_days'])}d)")
        else:
            for name in factor_names:
                ic, ric = calc_ic(fdf[name], fwd)
                p = summarize_ic(ic)
                s = summarize_ic(ric)
                print(f"  {name:<20s}  IC={_fmt(p['ic_mean']):>8s}  "
                      f"IR={_fmt(p['icir']):>7s}  |  RankIC={_fmt(s['ic_mean']):>8s}  "
                      f"RankIR={_fmt(s['icir']):>7s}  "
                      f"Win={_fmt(s['ic_positive_ratio']):>7s}  ({p['n_days']}d)")
            # 分层
            print(f"\n  分层回测（5 档，{args.forward}d 前瞻）")
            for name in factor_names:
                layers = calc_layered_returns(fdf[name], fwd, n_quantiles=5)
                if not layers:
                    continue
                qs = sorted(layers)
                line = "  ".join(
                    f"Q{q}:{layers[q].iloc[-1] - 1:+.3f}" for q in qs)
                ls = layers[qs[-1]].iloc[-1] - layers[qs[0]].iloc[-1]
                print(f"  {name:<20s}  {line}  |  多空 {ls:+.3f}")
        return 0
    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
