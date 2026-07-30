"""Brinson 行业归因 — 超额收益分解为行业配置效应 + 选股效应 + 交互效应。

独立工具，不进 btcore 引擎。从回测 DB 重建每日持仓，
结合申万行业分类与行业指数，对策略超额收益做 Brinson 分解。

用法：
    from research.attribution import brinson_attribute

    result = brinson_attribute(
        "backtest_output/run.db",
        "/path/to/market.db",
        "20240601", "20240701",
    )
    print(f"配置效应={result['summary']['allocation_effect']:.4%}")
    print(f"选股效应={result['summary']['selection_effect']:.4%}")

    也可从本地 parquet 文件加载数据（无需外部数据库）：
    result = brinson_attribute_from_files(
        "backtest_output/run.db",
        industry_map="brinson_data/industry_map.parquet",
        sw_returns="brinson_data/sw_returns.parquet",
        benchmark_weights="brinson_data/benchmark_weights.parquet",
        bars="brinson_data/bars.parquet",
    )
    先用 scripts/dump_brinson_data.py 导出 parquet 文件。

数据源：
    - index_member_all  → 股票→申万行业映射 (ts_code → l1_code/l1_name)
    - sw_daily          → 申万行业指数日线 (pct_change 做行业基准收益)
    - index_weight      → 基准指数成分股权重
    - 回测 DB trade_log → 买卖流水 (重建每日持仓)
    - stk_factor_pro    → 个股日线 (close 算市值, pct_chg 算个股收益)
"""

import logging
import sqlite3
from collections import defaultdict
from itertools import groupby

import pandas as pd

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════
# 数据加载
# ═══════════════════════════════════════════


def _open_provider_db(db_path: str) -> sqlite3.Connection:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_industry_map(conn: sqlite3.Connection) -> dict[str, str]:
    """读取 index_member_all，返回 {ts_code: l1_name}。"""
    rows = conn.execute(
        "SELECT ts_code, l1_name FROM index_member_all"
    ).fetchall()
    result = {}
    for r in rows:
        result[r["ts_code"]] = r["l1_name"]
    logger.info("industry_map: %d stocks → %d industries",
                len(result), len(set(result.values())))
    return result


def _load_sw_returns(
    conn: sqlite3.Connection,
    start: str,
    end: str,
    l1_codes: list[str],
) -> pd.DataFrame:
    """读取 sw_daily L1 行业指数日收益率。

    Args:
        conn: 数据库只读连接。
        start/end: 日期范围。
        l1_codes: L1 行业指数代码列表 (如 ["801780.SI", "801010.SI"])。

    Returns:
        DataFrame，index=date，columns=行业名，values=日收益率 (小数)。
    """
    if not l1_codes:
        return pd.DataFrame()
    placeholders = ",".join("?" * len(l1_codes))
    params = list(l1_codes) + [start, end]
    sql = (
        "SELECT trade_date, ts_code, name, pct_change FROM sw_daily "
        f"WHERE ts_code IN ({placeholders}) "
        "AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date"
    )
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        logger.warning("sw_daily: 无 L1 行业数据 (%s~%s)", start, end)
        return pd.DataFrame()

    records = []
    for r in rows:
        records.append({
            "date": r["trade_date"],
            "industry": r["name"],
            "ret": (r["pct_change"] or 0.0) / 100.0,
        })

    df = pd.DataFrame(records)
    pivoted = df.pivot_table(
        index="date", columns="industry", values="ret", aggfunc="first"
    )
    logger.info("sw_returns: %d days × %d industries", *pivoted.shape)
    return pivoted


def _load_benchmark_weights(
    conn: sqlite3.Connection,
    index_code: str,
    start: str,
    end: str,
    industry_map: dict[str, str],
) -> pd.DataFrame:
    """读取 index_weight，映射到行业，计算每日基准行业权重。

    Returns:
        DataFrame，index=date，columns=行业名，values=权重 (0~1)。
    """
    rows = conn.execute(
        "SELECT con_code, trade_date, weight FROM index_weight "
        "WHERE index_code=? AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date",
        (index_code, start, end),
    ).fetchall()

    if not rows:
        logger.warning("index_weight: 无 %s 数据 (%s~%s)", index_code, start, end)
        return pd.DataFrame()

    records = []
    unmapped = set()
    for r in rows:
        industry = industry_map.get(r["con_code"])
        if industry is None:
            unmapped.add(r["con_code"])
            continue
        records.append({
            "date": r["trade_date"],
            "industry": industry,
            "weight": r["weight"] or 0.0,
        })

    if unmapped:
        logger.warning("benchmark_weights: %d symbols 无行业映射", len(unmapped))

    df = pd.DataFrame(records)
    # 每日各行业权重 = sum(成分股权重)
    grouped = df.groupby(["date", "industry"])["weight"].sum().reset_index()
    # 归一化：每日权重和为 1
    daily_total = grouped.groupby("date")["weight"].sum()
    grouped["weight"] = grouped.apply(
        lambda row: row["weight"] / daily_total[row["date"]], axis=1
    )
    pivoted = grouped.pivot_table(
        index="date", columns="industry", values="weight", aggfunc="first", fill_value=0.0
    )
    logger.info("benchmark_weights: %d days × %d industries", *pivoted.shape)
    return pivoted


# ═══════════════════════════════════════════
# 持仓重建
# ═══════════════════════════════════════════


def _reconstruct_daily_holdings(
    trade_log_df: pd.DataFrame,
    bars_df: pd.DataFrame,
    industry_map: dict[str, str],
) -> pd.DataFrame:
    """从 trade_log 回放重建每日持仓，计算行业权重和行业收益。

    Args:
        trade_log_df: 回测 trade_log 表，含 date/symbol/side/shares。
        bars_df: stk_factor_pro 数据，MultiIndex (trade_date, symbol)，含 close/pct_chg。
        industry_map: {ts_code: industry_name}。

    Returns:
        DataFrame，index=date，每行含：
        - {industry}_weight: 该日该行业持仓权重
        - {industry}_return: 该日该行业市值加权收益率
        - portfolio_return: 该日策略总收益率
    """
    if trade_log_df.empty:
        logger.warning("trade_log 为空，无法重建持仓")
        return pd.DataFrame()

    # trade_log 已按 (date, id) 排序（SQL ORDER BY）；按日期分组，避免逐日全表扫描
    trades_by_date = {
        d: list(g) for d, g in groupby(
            trade_log_df.to_dict("records"), key=lambda t: t["date"]
        )
    }

    holdings: dict[str, int] = {}  # symbol → current shares
    results: list[dict] = []

    # 口径：只遍历 trade_log 出现过的日期；个股当日缺 bar（如停牌）时该股当天市值按缺失处理
    for date in sorted(trades_by_date):
        # 处理当日买卖
        for t in trades_by_date[date]:
            sym = t["symbol"]
            if t["side"] == "BUY":
                holdings[sym] = holdings.get(sym, 0) + t["shares"]
            elif t["side"] == "SELL":
                holdings[sym] = holdings.get(sym, 0) - t["shares"]
                if holdings[sym] <= 0:
                    holdings.pop(sym, None)
            # DIV side 不影响持仓股数

        # 过滤零/负持仓
        active = {s: q for s, q in holdings.items() if q > 0}
        if not active:
            results.append({"date": date, "portfolio_return": 0.0})
            continue

        # 查当日 bars: 每只持仓股的 close 和 pct_chg
        row: dict = {"date": date}
        industry_mv: dict[str, float] = defaultdict(float)
        industry_wgt_ret: dict[str, float] = defaultdict(float)
        total_mv = 0.0
        unmapped = set()

        for sym, shares in active.items():
            industry = industry_map.get(sym)
            if industry is None:
                unmapped.add(sym)
                continue

            try:
                bar = bars_df.loc[(date, sym)]
            except KeyError:
                continue

            close = float(bar["close"])
            pct_chg_raw = bar.get("pct_chg")
            pct_chg = (
                float(pct_chg_raw) / 100.0
                if pct_chg_raw and pct_chg_raw == pct_chg_raw
                else 0.0
            )

            mv = shares * close
            industry_mv[industry] += mv
            total_mv += mv

            # 市值加权收益率累加
            industry_wgt_ret[industry] += mv * pct_chg

        if unmapped:
            logger.warning("[%s] %d symbols 无行业映射: %s", date, len(unmapped),
                           ",".join(sorted(unmapped)[:5]))

        if total_mv <= 0:
            row["portfolio_return"] = 0.0
            results.append(row)
            continue

        # 计算行业权重和收益率
        for industry, mv in industry_mv.items():
            weight = mv / total_mv
            ret = industry_wgt_ret[industry] / mv if mv > 0 else 0.0
            row[f"{industry}_weight"] = weight
            row[f"{industry}_return"] = ret

        results.append(row)

    result_df = pd.DataFrame(results).set_index("date")
    logger.info("holdings_reconstructed: %d days", len(result_df))
    return result_df


# ═══════════════════════════════════════════
# Brinson 计算
# ═══════════════════════════════════════════


def _compute_brinson_daily(
    holdings_df: pd.DataFrame,
    benchmark_weights: pd.DataFrame,
    sw_returns: pd.DataFrame,
) -> pd.DataFrame:
    """逐日计算 Brinson 三效应。

    Args:
        holdings_df: _reconstruct_daily_holdings 的输出。
        benchmark_weights: _load_benchmark_weights 的输出。
        sw_returns: _load_sw_returns 的输出。

    Returns:
        DataFrame，index=date，含 allocation/selection/interaction 等列。
    """
    # 对齐日期
    common_dates = holdings_df.index.intersection(benchmark_weights.index)
    common_dates = common_dates.intersection(sw_returns.index)
    common_dates = sorted(common_dates)

    if not common_dates:
        logger.warning("Brinson: 三方数据无交叠日期")
        return pd.DataFrame()

    # 提取列名模式: 行业名从 holdings_df 的 {ind}_weight 列提取
    industry_names = set()
    for col in holdings_df.columns:
        if col.endswith("_weight"):
            industry_names.add(col[:-7])  # 去掉 "_weight"

    if not industry_names:
        logger.warning("Brinson: 无行业持仓数据")
        return pd.DataFrame()

    daily_results = []

    for date in common_dates:
        # common_dates 已是三方索引交集，直接 .loc 取值
        w_b_row = benchmark_weights.loc[date]
        sw_row = sw_returns.loc[date]
        h_row = holdings_df.loc[date]

        # 单遍提取每行业的 (基准权重, 行业基准收益, 策略权重, 策略收益)
        ind_data = [
            (
                float(w_b_row.get(ind, 0.0)),
                float(sw_row.get(ind, 0.0)),
                float(h_row.get(f"{ind}_weight", 0.0)),
                float(h_row.get(f"{ind}_return", 0.0)),
            )
            for ind in industry_names
        ]

        # 口径：基准收益只在策略持仓出现过的行业上累加，未持有行业的基准权重
        # 不进入 benchmark_return 与配置效应（疑似方法论缺陷，本次只显式化不改行为）
        r_b_total = 0.0
        for w_b, r_b_i, _w_p, _r_p_i in ind_data:
            r_b_total += w_b * r_b_i

        allocation = 0.0
        selection = 0.0
        interaction = 0.0
        for w_b, r_b_i, w_p, r_p_i in ind_data:
            selection += w_b * (r_p_i - r_b_i)
            interaction += (w_p - w_b) * (r_p_i - r_b_i)
            allocation += (w_p - w_b) * (r_b_i - r_b_total)

        r_p_total = float(h_row.get("portfolio_return", 0.0))

        daily_results.append({
            "date": date,
            "portfolio_return": r_p_total,
            "benchmark_return": r_b_total,
            "excess_return": r_p_total - r_b_total,
            "allocation": allocation,
            "selection": selection,
            "interaction": interaction,
            "unexplained": (r_p_total - r_b_total) - (allocation + selection + interaction),
        })

    return pd.DataFrame(daily_results).set_index("date")


# ═══════════════════════════════════════════
# 聚合
# ═══════════════════════════════════════════


def _aggregate_period(
    daily_df: pd.DataFrame,
    holdings_df: pd.DataFrame,
    benchmark_weights: pd.DataFrame,
    sw_returns: pd.DataFrame,
) -> dict:
    """从每日 Brinson 数据聚合全周期 summary + industry_detail + exposure_summary。

    Returns:
        dict 含 summary / industry_detail / daily / exposure_summary。
    """
    if daily_df.empty:
        return {"summary": {}, "industry_detail": {}, "daily": [], "exposure_summary": {}}

    summary = {
        "total_portfolio_return": round(float(daily_df["portfolio_return"].sum()), 8),
        "total_benchmark_return": round(float(daily_df["benchmark_return"].sum()), 8),
        "total_excess_return": round(float(daily_df["excess_return"].sum()), 8),
        "allocation_effect": round(float(daily_df["allocation"].sum()), 8),
        "selection_effect": round(float(daily_df["selection"].sum()), 8),
        "interaction_effect": round(float(daily_df["interaction"].sum()), 8),
        "unexplained": round(float(daily_df["unexplained"].sum()), 8),
    }

    # industry_detail: 每个行业的平均权重、累计收益、三效应分解
    industry_names = set()
    for col in holdings_df.columns:
        if col.endswith("_weight"):
            industry_names.add(col[:-7])

    industry_detail = {}
    for ind in sorted(industry_names):
        w_p_series = holdings_df.get(f"{ind}_weight", pd.Series(dtype=float))
        w_b_series = benchmark_weights.get(ind, pd.Series(dtype=float))
        r_b_series = sw_returns.get(ind, pd.Series(dtype=float))

        avg_w_p = float(w_p_series.mean()) if len(w_p_series) > 0 else 0.0
        avg_w_b = float(w_b_series.mean()) if len(w_b_series) > 0 else 0.0

        # 行业层面 Brinson（对齐日期的累计）
        idx_sets = (
            set(w_p_series.index) & set(w_b_series.index)
            & set(r_b_series.index) & set(daily_df.index)
        )
        common_dates = sorted(idx_sets)

        # 用 daily_df 里每天的基准总收益来修正
        port_ret = 0.0
        bench_ret = 0.0
        alloc = 0.0
        selec = 0.0
        inter = 0.0

        for date in common_dates:
            w_p = float(w_p_series.get(date, 0.0))
            w_b = float(w_b_series.get(date, 0.0))
            r_b_i = float(r_b_series.get(date, 0.0))
            # common_dates 已含于 daily_df.index，直接取当日基准总收益
            r_b = float(daily_df.loc[date, "benchmark_return"])

            # 策略在该行业的实际收益 = 从 holdings_df 取
            # (common_dates 已含于 holdings_df.index 且 {ind}_return 列与 {ind}_weight 列成对存在)
            r_p_i = float(holdings_df.loc[date, f"{ind}_return"])

            port_ret += w_p * r_p_i
            bench_ret += w_b * r_b_i
            alloc += (w_p - w_b) * (r_b_i - r_b)
            selec += w_b * (r_p_i - r_b_i)
            inter += (w_p - w_b) * (r_p_i - r_b_i)

        industry_detail[ind] = {
            "avg_portfolio_weight": round(avg_w_p, 6),
            "avg_benchmark_weight": round(avg_w_b, 6),
            "active_weight": round(avg_w_p - avg_w_b, 6),
            "portfolio_return": round(port_ret, 8),
            "benchmark_return": round(bench_ret, 8),
            "allocation_effect": round(alloc, 8),
            "selection_effect": round(selec, 8),
            "interaction_effect": round(inter, 8),
            "total_contribution": round(alloc + selec + inter, 8),
        }

    # exposure_summary
    avg_weights = {}
    for col in holdings_df.columns:
        if col.endswith("_weight"):
            ind = col[:-7]
            avg_weights[ind] = float(holdings_df[col].mean()) if len(holdings_df[col]) > 0 else 0.0

    if avg_weights:
        max_ind = max(avg_weights, key=avg_weights.get)
        # effective N = 1 / Σ(w²)
        sum_sq = sum(w * w for w in avg_weights.values())
        effective_n = 1.0 / sum_sq if sum_sq > 0 else 0.0
        top3 = sorted(avg_weights.items(), key=lambda x: x[1], reverse=True)[:3]
        exposure_summary = {
            "max_single_industry_weight": round(avg_weights[max_ind], 6),
            "max_single_industry_name": max_ind,
            "effective_n_industries": round(effective_n, 2),
            "top3_industries": [{"name": n, "weight": round(w, 6)} for n, w in top3],
        }
    else:
        exposure_summary = {}

    # daily 列表
    daily_list = []
    for date in sorted(daily_df.index):
        row = daily_df.loc[date]
        daily_list.append({
            "date": str(date),
            "portfolio_return": round(float(row["portfolio_return"]), 8),
            "benchmark_return": round(float(row["benchmark_return"]), 8),
            "excess_return": round(float(row["excess_return"]), 8),
            "allocation": round(float(row["allocation"]), 8),
            "selection": round(float(row["selection"]), 8),
            "interaction": round(float(row["interaction"]), 8),
        })

    return {
        "summary": summary,
        "industry_detail": industry_detail,
        "daily": daily_list,
        "exposure_summary": exposure_summary,
    }


# ═══════════════════════════════════════════
# 编排入口
# ═══════════════════════════════════════════


def brinson_attribute(
    db_path: str,
    provider_db: str,
    start: str,
    end: str,
    index_code: str = "000300.SH",
    run_id: int | None = None,
) -> dict:
    """Brinson 行业归因：分解策略超额收益。

    Args:
        db_path: 回测 DB 文件路径（含 trade_log/account_daily 表）。
        provider_db: 数据库路径（只读）。
        start/end: 回测起止日期 (YYYYMMDD)。
        index_code: 基准指数代码，默认 "000300.SH"。
        run_id: 指定分析的 run；None 时取最新 run (MAX(run_id))。
            旧库无 run_id 列时不做过滤。

    Returns:
        {
            "summary": {
                "total_portfolio_return": ..., "total_benchmark_return": ...,
                "total_excess_return": ..., "allocation_effect": ...,
                "selection_effect": ..., "interaction_effect": ..., "unexplained": ...
            },
            "industry_detail": {"银行": {...}, ...},
            "daily": [{...}, ...],
            "exposure_summary": {...}
        }
    """
    # 打开连接
    backtest_conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    provider_conn = _open_provider_db(provider_db)

    try:
        # 1. 加载行业映射
        industry_map = _load_industry_map(provider_conn)

        # L1 行业代码集合
        l1_codes = sorted(
            r[0]
            for r in provider_conn.execute(
                "SELECT DISTINCT l1_code FROM index_member_all"
            ).fetchall()
            if r[0]
        )

        # 2. 加载行业指数收益
        sw_returns = _load_sw_returns(provider_conn, start, end, l1_codes)

        if sw_returns.empty:
            return {"error": "sw_daily 无数据", "summary": {}, "industry_detail": {},
                    "daily": [], "exposure_summary": {}}

        # 3. 加载基准行业权重
        benchmark_weights = _load_benchmark_weights(
            provider_conn, index_code, start, end, industry_map
        )
        if benchmark_weights.empty:
            logger.warning("benchmark_weights 为空，配置效应和选股效应将无法计算")

        # 4. 读取回测 trade_log（按 run_id 过滤；旧库无该列则不过滤）
        run_cols = {
            row[1]
            for row in backtest_conn.execute(
                "PRAGMA table_info(trade_log)"
            ).fetchall()
        }
        run_filter = ""
        run_params: list = []
        if "run_id" in run_cols:
            if run_id is None:
                # 聚合查询 fetchone 恒返回一行，None 风险只在 row[0]
                run_id = backtest_conn.execute(
                    "SELECT MAX(run_id) FROM runs"
                ).fetchone()[0]
            if run_id is not None:
                run_filter = " AND run_id = ?"
                run_params = [run_id]

        trade_log_df = pd.read_sql_query(
            "SELECT id, date, symbol, side, shares FROM trade_log "
            "WHERE date >= ? AND date <= ? AND side IN ('BUY', 'SELL')"
            f"{run_filter} ORDER BY date, id",
            backtest_conn, params=(start, end, *run_params),
        )

        # 5. 加载 bars 数据（只拉实际交易过的股票）
        traded_symbols = sorted(trade_log_df["symbol"].unique())
        if len(traded_symbols) == 0:
            return {"error": "trade_log 无买卖记录", "summary": {}, "industry_detail": {},
                    "daily": [], "exposure_summary": {}}

        # 从数据库拉 bars
        bars_df = _load_bars_for_symbols(provider_conn, traded_symbols, start, end)
        if bars_df.empty:
            return {"error": "bars 数据为空", "summary": {}, "industry_detail": {},
                    "daily": [], "exposure_summary": {}}

        # 6. 重建每日持仓（行业权重 + 行业收益）
        holdings_df = _reconstruct_daily_holdings(trade_log_df, bars_df, industry_map)
        if holdings_df.empty:
            return {"error": "持仓重建失败", "summary": {}, "industry_detail": {},
                    "daily": [], "exposure_summary": {}}

        # 7. Brinson 逐日计算
        daily_df = _compute_brinson_daily(holdings_df, benchmark_weights, sw_returns)

        # 8. 聚合
        result = _aggregate_period(daily_df, holdings_df, benchmark_weights, sw_returns)

        return result

    finally:
        backtest_conn.close()
        provider_conn.close()


def _load_bars_for_symbols(
    conn: sqlite3.Connection,
    symbols: list[str],
    start: str,
    end: str,
) -> pd.DataFrame:
    """从 stk_factor_pro 拉取指定股票在日期范围内的 bars。"""
    placeholders = ",".join("?" * len(symbols))
    sql = (
        "SELECT ts_code AS symbol, trade_date, close, pct_chg FROM stk_factor_pro "
        f"WHERE ts_code IN ({placeholders}) "
        "AND trade_date >= ? AND trade_date <= ? "
        "ORDER BY trade_date, ts_code"
    )
    params = list(symbols) + [start, end]
    df = pd.read_sql_query(sql, conn, params=params)
    if df.empty:
        return df
    df.set_index(["trade_date", "symbol"], inplace=True)
    df.sort_index(inplace=True)
    logger.info("bars: %d rows, %d symbols", len(df), len(symbols))
    return df




# ═══════════════════════════════════════════
# 本地文件入口
# ═══════════════════════════════════════════


def brinson_attribute_from_files(
    result_db: str,
    industry_map: str,
    sw_returns: str,
    benchmark_weights: str,
    bars: str,
    run_id: int = 1,
    benchmark_code: str = "000300.SH",
) -> dict:
    """从本地 parquet 文件做 Brinson 归因（无需外部数据库）。

    Args:
        result_db: 回测结果数据库路径。
        industry_map: 行业映射 parquet (columns: ts_code, l1_name)。
        sw_returns: 申万行业日收益 parquet (index=date, columns=行业名, values=小数)。
        benchmark_weights: 基准行业权重 parquet (index=date, columns=行业名, values=0~1)。
        bars: 个股 bars parquet (MultiIndex trade_date,symbol; columns: close, pct_chg)。
        run_id: 回测 run_id。
        benchmark_code: 基准指数代码（仅用于日志标注）。

    Returns:
        与 brinson_attribute() 相同结构的归因报告。
    """
    import os

    # 校验文件存在
    for label, path in [
        ("industry_map", industry_map),
        ("sw_returns", sw_returns),
        ("benchmark_weights", benchmark_weights),
        ("bars", bars),
    ]:
        if not os.path.exists(path):
            raise FileNotFoundError(f"{label} 文件不存在: {path}")

    # 1. 加载行业映射 parquet → {ts_code: l1_name}
    ind_df = pd.read_parquet(industry_map)
    if "ts_code" not in ind_df.columns or "l1_name" not in ind_df.columns:
        raise ValueError("industry_map parquet 需含 ts_code 和 l1_name 列")
    industry_map_dict: dict[str, str] = dict(
        zip(ind_df["ts_code"], ind_df["l1_name"])
    )
    logger.info(
        "industry_map (from files): %d stocks → %d industries",
        len(industry_map_dict),
        len(set(industry_map_dict.values())),
    )

    # 2. 加载行业指数收益 parquet
    sw_df = pd.read_parquet(sw_returns)
    if sw_df.empty:
        return {
            "error": "sw_returns 为空",
            "summary": {},
            "industry_detail": {},
            "daily": [],
            "exposure_summary": {},
        }
    logger.info("sw_returns (from files): %d days × %d industries", *sw_df.shape)

    # 3. 加载基准行业权重 parquet
    bw_df = pd.read_parquet(benchmark_weights)
    if bw_df.empty:
        logger.warning("benchmark_weights (from files) 为空，配置效应和选股效应将无法计算")
    else:
        logger.info("benchmark_weights (from files): %d days × %d industries", *bw_df.shape)

    # 4. 加载 bars parquet（需 MultiIndex trade_date, symbol）
    bars_df = pd.read_parquet(bars)
    if isinstance(bars_df.index, pd.MultiIndex):
        pass  # 已经是 MultiIndex
    elif "trade_date" in bars_df.columns and "symbol" in bars_df.columns:
        bars_df = bars_df.set_index(["trade_date", "symbol"])
    else:
        raise ValueError(
            "bars parquet 需含 MultiIndex (trade_date, symbol) 或对应的列"
        )
    if bars_df.empty:
        return {
            "error": "bars 数据为空",
            "summary": {},
            "industry_detail": {},
            "daily": [],
            "exposure_summary": {},
        }
    logger.info("bars (from files): %d rows", len(bars_df))

    # 5. 读取回测 trade_log
    backtest_conn = sqlite3.connect(f"file:{result_db}?mode=ro", uri=True)
    try:
        trade_log_df = pd.read_sql_query(
            "SELECT id, date, symbol, side, shares FROM trade_log "
            "WHERE side IN ('BUY', 'SELL') AND run_id = ? ORDER BY date, id",
            backtest_conn,
            params=(run_id,),
        )
    finally:
        backtest_conn.close()

    if trade_log_df.empty:
        return {
            "error": "trade_log 无买卖记录",
            "summary": {},
            "industry_detail": {},
            "daily": [],
            "exposure_summary": {},
        }

    # 6. 重建每日持仓
    holdings_df = _reconstruct_daily_holdings(trade_log_df, bars_df, industry_map_dict)
    if holdings_df.empty:
        return {
            "error": "持仓重建失败",
            "summary": {},
            "industry_detail": {},
            "daily": [],
            "exposure_summary": {},
        }

    # 7. Brinson 逐日计算
    daily_df = _compute_brinson_daily(holdings_df, bw_df, sw_df)

    # 8. 聚合
    return _aggregate_period(daily_df, holdings_df, bw_df, sw_df)
