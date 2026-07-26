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
