"""因子数据供给规划与物化 — 引擎 preload 的纯函数助手。

同时承载面板准备的共享助手（必需列契约校验、派生列、伪列附着、
基准收益派生），引擎 preload 与训练侧（btcore.ml.dataset）/
研究脚本（scripts/factor_eval.py）共用同一套函数，保证训练与
回测的数据准备口径逐列一致。

从策略引用的因子闭包静态推导两路数据供给计划（build_factor_plan）：
  - 主面板：候选池 × 长窗口（max(365 天, 闭包最大 ts 窗口换算)）× 基础列
  - 广度面板：全市场 × 短窗口 × 窄列（仅当闭包含坍缩算子节点；
    瞬时加载，物化投影后由引擎释放）

物化（materialize）在引擎 preload 后、attach_bars 前一次性执行：
广度闭包节点先在广度面板上按拓扑序求值，坍缩节点投影回主面板
（market → 按 date 广播；group → 按 (date, industry) map），随后
主面板节点按拓扑序物化为新列。所有算子都是因果的（rolling / 截面
聚合只用 ≤ 当日数据），物化列与 *_hfq 派生同构，无前视。

口径语义：保形 xsec 算子在主面板（候选池并集）上逐日计算；
坍缩 xsec 算子在广度面板（全市场）上逐日聚合后投影。

纯函数模块：不依赖 engine / match / database / provider。
"""

import numpy as np
import pandas as pd

from btcore.factors import cse, ops
from btcore.factors.expr import evaluate_expr, extract_expr_names
from btcore.factors.library import spec_names

# 数据契约必需列（docs/backend_guide.md）——缺列直接报错，不走语义不精确的兜底
# amount 不在其中：引擎内部不消费，仅为策略 select() 提供，策略通过 REQUIRED_FIELDS 声明
REQUIRED_BAR_COLUMNS = (
    "open", "high", "low", "close",
    "vol",          # 单位: 手 (1 手 = 100 股)
    "adj_factor",
    "pre_close",    # 交易所除权调整口径: 除权日 = (前裸收盘 - 现金分红) / (1 + 送转比例)
    "up_limit", "down_limit",
)

# 伪列：派生/附着，不向 backend 请求
PSEUDO_COLUMNS = frozenset({"idx_ret", "log_mktcap", "industry"})

# 主面板 warmup 地板（日历天）：因子窗口再小也预加载一年历史，
# 供策略 select() 命令式读取历史 bar；窗口推导只覆盖因子物化需求
DEFAULT_WARMUP_DAYS = 365

# 派生列：derive_fields 从基础列计算，不向 backend 请求
DERIVED_BASES: dict[str, frozenset[str]] = {
    "open_hfq": frozenset({"open", "adj_factor"}),
    "high_hfq": frozenset({"high", "adj_factor"}),
    "low_hfq": frozenset({"low", "adj_factor"}),
    "close_hfq": frozenset({"close", "adj_factor"}),
    "pct_chg": frozenset({"close", "pre_close"}),
}


def expand_columns(columns) -> list[str]:
    """请求列展开：派生列替换为基础列，伪列丢弃。

    引擎 preload 与 scripts/factor_eval.py 共用，确保 query_bars
    只请求 backend 能提供的列。
    """
    out: set[str] = set()
    for col in columns:
        if col in DERIVED_BASES:
            out |= DERIVED_BASES[col]
        elif col not in PSEUDO_COLUMNS:
            out.add(col)
    return sorted(out)


def validate_required_columns(bars_df: pd.DataFrame) -> None:
    """契约强校验：缺必需列直接失败。

    pre_close / up_limit / down_limit 曾允许引擎兜底推算，但兜底语义不精确
    （除权日涨跌停一阶错误、pct_chg 假暴跌），故改为数据契约强制提供。
    """
    missing = [c for c in REQUIRED_BAR_COLUMNS if c not in bars_df.columns]
    if missing:
        raise ValueError(
            f"bars 缺必需列: {missing}, 数据契约见 docs/backend_guide.md"
        )


def derive_fields(bars_df: pd.DataFrame) -> None:
    """补齐可由基础列精确派生的字段（原地写列）。

    *_hfq = 裸价 × adj_factor（hfq 定义）；pct_chg 由 pre_close（交易所
    除权调整口径，必需列）派生。这两个派生都是精确的，无语义损耗。
    广度面板按列裁剪后可能只带部分基础列，缺基础列的派生直接跳过。
    """
    if "adj_factor" in bars_df.columns:
        for src, dst in [("open", "open_hfq"), ("high", "high_hfq"),
                         ("low", "low_hfq"), ("close", "close_hfq")]:
            if dst not in bars_df.columns and src in bars_df.columns:
                bars_df[dst] = bars_df[src] * bars_df["adj_factor"]

    if ("pct_chg" not in bars_df.columns
            and {"close", "pre_close"} <= set(bars_df.columns)):
        pre = bars_df["pre_close"]
        bars_df["pct_chg"] = (bars_df["close"] - pre) / pre.replace(0, np.nan)


def derive_idx_ret(df: pd.DataFrame, backend, benchmark: str | None) -> pd.Series:
    """指数参照序列（benchmark hfq_close 的日收益）按日期广播进面板。"""
    bench_fn = getattr(backend, "get_benchmark_bars", None)
    if not (callable(bench_fn) and benchmark):
        raise ValueError(
            "因子引用 idx_ret 需要 benchmark 且 backend 提供 get_benchmark_bars"
        )
    dates = df.index.get_level_values("trade_date")
    bench = bench_fn(benchmark, dates.min(), dates.max())
    if bench is None or bench.empty:
        raise ValueError(f"基准 {benchmark} 无数据, 无法派生 idx_ret")
    ret = bench["hfq_close"].pct_change()
    ret.index = pd.Index(pd.to_datetime(ret.index).strftime("%Y%m%d"))
    return dates.map(ret)


def ensure_pseudo_columns(
    df: pd.DataFrame,
    needs: dict,
    panel: str,
    *,
    backend,
    benchmark: str | None = None,
    derive_idx_ret_fn=None,
) -> None:
    """按需附着伪列：industry / log_mktcap / idx_ret（原地写列）。

    引擎 preload 与训练侧/研究脚本共用，backend 为鸭子类型（只需有对应方法）。
    idx_ret 默认用 derive_idx_ret 派生；调用方可传 derive_idx_ret_fn 覆盖。
    """
    if needs.get(f"industry_{panel}"):
        fn = getattr(backend, "get_stock_industries", None)
        if not callable(fn):
            raise ValueError(
                "因子引用 industry 分组需要 backend 提供 get_stock_industries"
            )
        symbols = df.index.get_level_values("symbol").unique().tolist()
        mapping = fn(symbols)
        df["industry"] = df.index.get_level_values("symbol").map(mapping)
    if needs.get(f"mktcap_{panel}"):
        total_mv = df["total_mv"]
        df["log_mktcap"] = np.log(total_mv.where(total_mv > 0))
    if needs.get("index"):
        fn = derive_idx_ret_fn
        if fn is None:
            def fn(d):
                return derive_idx_ret(d, backend, benchmark)
        df["idx_ret"] = fn(df)


# 交易日窗口 → 日历天的工程换算（×1.5 + 缓冲）
def _to_calendar_days(trading_rows: int) -> int:
    return int(trading_rows * 1.5) + 10


def build_factor_plan(nodes: dict[str, dict], entry_names: list[str]) -> dict:
    """从因子闭包推导两路供给计划。

    nodes: {name: {expr, where?}}（library.resolve_closure 的输出）；
    entry_names: 策略直接引用的因子名（闭包入口）。

    返回 dict：
      topo:             闭包拓扑序（引用先于被引用方）
      breadth:          需在广度面板计算的节点集合
      collapse:         {坍缩节点: "market"|"group"}（投影方式）
      main_columns:     主面板需向 backend 请求的基础列（含伪列名，引擎再分流）
      breadth_columns:  广度面板基础列（同上）
      needs:            {market, index, industry_main, industry_breadth,
                         mktcap_main, mktcap_breadth} 布尔标志
      main_days / breadth_days: 两面板 warmup 日历天
      windows:          {节点名: 所需历史行数}（含引用传递；供 warmup 诊断）
      nodes:            CSE 重写后的节点（materialize 以此为准）
      cse_temp:         CSE 合成节点名列表（物化后删除临时列）
    """
    original = set(nodes)
    nodes = cse.rewrite(nodes)
    cse_temp = sorted(set(nodes) - original)
    # 闭包裁剪：只保留从入口可达的节点（容忍传入超集）
    reachable: set[str] = set()
    stack = [n for n in entry_names if n in nodes]
    while stack:
        name = stack.pop()
        if name in reachable:
            continue
        reachable.add(name)
        _, refs = spec_names(nodes[name], set(nodes))
        stack.extend(refs)
    nodes = {n: nodes[n] for n in reachable}
    names = set(nodes)
    order = _topo_order(nodes, entry_names)
    windows = infer_windows(nodes)
    max_window = max(windows.values(), default=1)

    # 广度集合 = 坍缩节点及其传递引用闭包（在广度面板上计算）；
    # 主面板集合 = 全部非坍缩节点（坍缩节点的值由投影提供）。
    # 被两侧同时引用的节点在两个面板各算一次（幂等，代价可忽略）。
    collapse: dict[str, str] = {}
    for name in order:
        kind = ops.collapse_kind(nodes[name]["expr"]) \
            if ops.has_op_call(nodes[name]["expr"]) else None
        if kind:
            collapse[name] = kind
    breadth: set[str] = set()
    stack = list(collapse)
    while stack:
        name = stack.pop()
        if name in breadth:
            continue
        breadth.add(name)
        _, refs = spec_names(nodes[name], names)
        stack.extend(refs)
    main_set = names - set(collapse)

    main_raw: set[str] = set()
    breadth_raw: set[str] = set()
    for name in order:
        cols, _ = spec_names(nodes[name], names)
        if name in breadth:
            breadth_raw |= cols
        if name in main_set:
            main_raw |= cols

    needs = {
        "market": bool(collapse),
        "index": "idx_ret" in main_raw or "idx_ret" in breadth_raw,
        "industry_main": "industry" in main_raw
        or any(k == "group" for k in collapse.values()),
        "industry_breadth": "industry" in breadth_raw,
        "mktcap_main": "log_mktcap" in main_raw,
        "mktcap_breadth": "log_mktcap" in breadth_raw,
    }
    # log_mktcap 由 total_mv 派生：被引用的面板补请求 total_mv
    if needs["mktcap_main"]:
        main_raw.add("total_mv")
    if needs["mktcap_breadth"]:
        breadth_raw.add("total_mv")

    breadth_days = _to_calendar_days(max_window)
    return {
        "topo": order,
        "main": main_set,
        "breadth": breadth,
        "collapse": collapse,
        "main_columns": main_raw - PSEUDO_COLUMNS,
        "breadth_columns": breadth_raw - PSEUDO_COLUMNS,
        "needs": needs,
        "windows": windows,
        "nodes": nodes,
        "cse_temp": cse_temp,
        "main_days": max(DEFAULT_WARMUP_DAYS, breadth_days),
        "breadth_days": breadth_days,
    }


def infer_windows(nodes: dict[str, dict]) -> dict[str, int]:
    """推导闭包每节点所需历史行数（交易日），含命名引用传递窗口。

    build_factor_plan 与 library.compute_breadth 共用同一份推导，避免两份
    逻辑漂移。拓扑序保证引用的窗口先算；纯表达式与 xsec 算子不消耗时间轴，
    ts 算子按其 window_cost 累加。
    """
    names = set(nodes)
    order = _topo_order(nodes, list(names))
    windows: dict[str, int] = {}
    for name in order:
        spec = nodes[name]
        if ops.has_op_call(spec["expr"]):
            w = ops.infer_window(spec["expr"], windows)
        else:
            refs = extract_expr_names(spec["expr"]) & names
            w = max([windows.get(r, 1) for r in refs], default=1)
        where = spec.get("where")
        if where:
            if ops.has_op_call(where):
                w = max(w, ops.infer_window(where, windows))
            else:
                refs = extract_expr_names(where) & names
                w = max([w, *[windows.get(r, 1) for r in refs]])
        windows[name] = w
    return windows


def materialize(
    main_df: pd.DataFrame,
    breadth_df: pd.DataFrame | None,
    plan: dict,
    nodes: dict[str, dict],
) -> None:
    """两阶段物化：广度面板求值 + 坍缩投影 + 主面板求值（原地写列）。

    nodes 以 plan["nodes"]（CSE 重写后）为准；CSE 合成节点的临时列在
    物化完成后删除。
    """
    nodes = plan.get("nodes", nodes)
    breadth_set: set[str] = plan["breadth"]
    if breadth_df is not None:
        for name in plan["topo"]:
            if name in breadth_set:
                breadth_df[name] = _eval_spec_on(breadth_df, nodes[name])
        for name, kind in plan["collapse"].items():
            _project(main_df, breadth_df, name, kind)
    main_set: set[str] = plan["main"]
    for name in plan["topo"]:
        if name in main_set:
            main_df[name] = _eval_spec_on(main_df, nodes[name])
    for tmp in plan.get("cse_temp", ()):
        main_df.drop(columns=tmp, inplace=True, errors="ignore")
        if breadth_df is not None:
            breadth_df.drop(columns=tmp, inplace=True, errors="ignore")


def validate_materialization(
    main_df: pd.DataFrame,
    plan: dict,
) -> list[dict]:
    """物化后验证：检查坍缩因子的完整性和数据质量（唯一验证入口）。

    Returns:
        list of dicts with keys: level (info/warning/error), message
    """
    import logging

    logger = logging.getLogger(__name__)
    issues = []

    for name in plan.get("collapse", {}):
        col = main_df.get(name)
        if col is None:
            msg = f"坍缩因子 {name!r} 未物化为主面板列"
            logger.warning(msg)
            issues.append({"level": "warning", "message": msg})
            continue
        nan_count = col.isna().sum()
        nan_pct = nan_count / len(col) if len(col) > 0 else 0
        nan_dates = (
            main_df.index[col.isna()].get_level_values("trade_date").nunique()
        )
        if nan_pct > 0.05:
            msg = (f"坍缩因子 {name!r} NaN 占比 {nan_pct:.1%} "
                   f"({nan_count}/{len(col)} 行, {nan_dates} 个交易日)")
            logger.warning(msg)
            issues.append({"level": "warning", "message": msg})
        elif nan_count:
            msg = (f"坍缩因子 {name!r} 有 {nan_count} 行 NaN "
                   f"({nan_dates} 个交易日)")
            logger.info(msg)
            issues.append({"level": "info", "message": msg})

    return issues


# ── 内部 ──


def _topo_order(nodes: dict[str, dict], entry_names: list[str]) -> list[str]:
    """Kahn 拓扑排序：引用先于被引用方。nodes 已是闭包，entry_names 仅作语义标注。"""
    names = set(nodes)
    deps = {name: spec_names(nodes[name], names)[1] for name in names}
    indegree = {name: 0 for name in names}
    rdeps: dict[str, set[str]] = {name: set() for name in names}
    for name, refs in deps.items():
        for ref in refs:
            indegree[name] += 1
            rdeps[ref].add(name)
    queue = sorted(n for n in names if indegree[n] == 0)
    order = []
    while queue:
        name = queue.pop(0)
        order.append(name)
        for dependent in sorted(rdeps[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(order) != len(names):
        raise ValueError(f"因子引用存在环: {sorted(names - set(order))}")
    return order


def _eval_spec_on(df: pd.DataFrame, spec: dict) -> pd.Series:
    if ops.has_op_call(spec["expr"]):
        values = ops.eval_op_expr(df, spec["expr"])
        where = spec.get("where")
        if where:
            values = values.where(ops.eval_op_expr(df, where).astype(bool))
    else:
        values = evaluate_expr(df, spec["expr"], where=spec.get("where"))
    return values


def _project(
    main_df: pd.DataFrame,
    breadth_df: pd.DataFrame,
    name: str,
    kind: str,
) -> None:
    """坍缩节点从广度面板投影回主面板（原地写列）。"""
    import logging

    main_dates = main_df.index.get_level_values("trade_date")
    if kind == "market":
        per_date = breadth_df[name].groupby(level="trade_date").first()
        main_df[name] = main_dates.map(per_date)
    else:
        per_group = breadth_df.groupby(
            [breadth_df.index.get_level_values("trade_date"), breadth_df["industry"]]
        )[name].first()
        key = pd.MultiIndex.from_arrays(
            [main_dates, main_df["industry"].to_numpy()]
        )
        main_df[name] = per_group.reindex(key).to_numpy()
    missing = main_dates[main_df[name].isna()].unique()
    if len(missing):
        logger = logging.getLogger(__name__)
        logger.warning("坍缩因子 %r 在 %d 个交易日无值: %s ...",
                       name, len(missing), missing[:5].tolist())
