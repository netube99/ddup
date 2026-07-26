"""因子数据供给规划与物化 — 引擎 preload 的纯函数助手。

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

import pandas as pd

from btcore.factors import ops
from btcore.factors.expr import evaluate_expr, extract_expr_names
from btcore.factors.library import spec_names

# 伪列：引擎派生/附着，不向 backend 请求
PSEUDO_COLUMNS = frozenset({"idx_ret", "log_mktcap", "industry"})

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
    """
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

    # 逐节点窗口（拓扑序保证引用的窗口先算）
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
            refs = extract_expr_names(where) & names
            w = max([w, *[windows.get(r, 1) for r in refs]])
        windows[name] = w
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
        "main_days": max(365, breadth_days),
        "breadth_days": breadth_days,
    }


def materialize(
    main_df: pd.DataFrame,
    breadth_df: pd.DataFrame | None,
    plan: dict,
    nodes: dict[str, dict],
) -> None:
    """两阶段物化：广度面板求值 + 坍缩投影 + 主面板求值（原地写列）。"""
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
    else:
        values = evaluate_expr(df, spec["expr"])
    where = spec.get("where")
    if where:
        values = values.where(df.eval(where))
    return values


def _project(
    main_df: pd.DataFrame,
    breadth_df: pd.DataFrame,
    name: str,
    kind: str,
) -> None:
    """坍缩节点从广度面板投影回主面板（原地写列）。"""
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
