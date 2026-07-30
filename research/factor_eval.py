"""因子评估工具 — IC 分析 / 分层回测。

纯函数式风格：指标计算是无状态纯函数，输入 pd.Series，输出标量/Series。
"""

import numpy as np
import pandas as pd


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
