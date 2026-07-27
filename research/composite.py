"""多因子合成 — 滚动 IC / ICIR 加权将多个因子合成为单一截面得分。

对标 hikyuu ICMultiFactor 的滚动 IC 加权思路：
每因子先截面 zscore 兜底标准化（去极值/中性化等深度处理应在
因子表达式内用 winsorize/neutralize 算子完成），再按滚动窗口内
因子 IC（或 ICIR）确定带符号权重，逐日加权合成。

前视约定：t 日权重只用 ≤ t-1 日的 IC 估计（rolling 后 shift(1)），
t 日 IC 依赖 t 日之后才能实现的收益，不可用于当日权重。

纯函数模块：不依赖 btcore / engine，输入输出均为 (trade_date, symbol)
MultiIndex 面板，与 research/factor_eval.py 同风格。
"""

import numpy as np
import pandas as pd

from research.factor_eval import calc_ic, calc_layered_returns, summarize_ic

_DATE = "trade_date"


def _xsec_zscore(s: pd.Series) -> pd.Series:
    g = s.groupby(level=_DATE, sort=False)
    std = g.transform("std")
    return (s - g.transform("mean")) / std.where(std != 0)


def combine_factors(
    factor_df: pd.DataFrame,
    forward_returns: pd.Series,
    method: str = "icir",
    window: int = 60,
    min_periods: int | None = None,
) -> pd.Series:
    """多因子合成截面得分。

    Args:
        factor_df: (trade_date, symbol) MultiIndex 宽表，每列一个因子值。
        forward_returns: 同结构的未来收益（仅 ic/icir 法用于权重估计）。
        method: "equal"（等权）| "ic"（滚动 IC 加权）| "icir"（滚动 ICIR 加权）。
        window: IC 估计的滚动窗口（交易日）。
        min_periods: 滚动窗口最少有效观测数，默认 max(2, window // 2)。

    Returns:
        composite 得分 Series（同索引）。前 ~window 日因权重不可估计为 NaN。
    """
    if method not in ("equal", "ic", "icir"):
        raise ValueError(f"未知合成方法: {method!r}，支持 equal/ic/icir")
    if factor_df.empty:
        return pd.Series(dtype=float, name="composite")

    z = factor_df.apply(_xsec_zscore)
    if method == "equal":
        return z.mean(axis=1).rename("composite")

    mp = min_periods if min_periods is not None else max(2, window // 2)
    ic_df = pd.DataFrame({c: calc_ic(z[c], forward_returns)[0] for c in z.columns})
    mean = ic_df.rolling(window, min_periods=mp).mean().shift(1)
    if method == "ic":
        w = mean
    else:
        std = ic_df.rolling(window, min_periods=mp).std().shift(1)
        w = mean / std.where(std != 0)
    denom = w.abs().sum(axis=1)
    w = w.div(denom.where(denom != 0), axis=0)
    # 部分因子在某日无 IC 时填零（不污染整日合成）；全部因子无 IC 的行保持 NaN（warmup 期）
    valid_rows = w.notna().any(axis=1)
    w = w.fillna(0.0).where(valid_rows, axis=0)

    # 逐日截面权重广播到个股（同日同权重）
    dates = z.index.get_level_values(_DATE).astype(str)  # 归一化防止 Timestamp→str map 静默 NaN
    comp = pd.Series(0.0, index=z.index)
    for c in z.columns:
        w_col = pd.Series(np.asarray(dates.map(w[c]), dtype=float), index=z.index)
        comp = comp + z[c] * w_col      # 权重或因子缺失日自然传播 NaN
    return comp.rename("composite")


def evaluate_composite(
    composite: pd.Series,
    forward_returns: pd.Series,
    n_quantiles: int = 10,
) -> dict:
    """合成因子评估：IC / RankIC 汇总 + 分层累计收益。

    Returns:
        {"ic": summarize_ic 汇总, "rank_ic": 同左,
         "layered": {分位: 累计收益 Series}}
    """
    ic, ric = calc_ic(composite, forward_returns)
    return {
        "ic": summarize_ic(ic),
        "rank_ic": summarize_ic(ric),
        "layered": calc_layered_returns(composite, forward_returns, n_quantiles),
    }
