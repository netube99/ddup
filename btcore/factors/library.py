"""因子库机制 — 加载与计算顶层 factors/library.yaml 里的因子定义。

因子定义（纯数据）住在顶层 factors/library.yaml；本模块只提供纯函数
机制：无类、无注册表、无全局可变状态。research 和 strategies 按名字
消费同一份定义、同一条计算路径。

统一模型：因子 = (trade_date, symbol) 面板上的命名计算 DAG。
一条 expr 可以是逐行表达式（pandas.eval 路径，见 expr.py），也可以
含白名单算子调用（ts 沿时间轴 / xsec 沿股票轴，见 ops.py）——两种
形态可嵌套、可按名字引用其他因子节点。加载期逐条校验并对全库做
环检测；where 统一为求值后掩码（False → NaN），不删行、不破坏
ts 窗口。
"""

from pathlib import Path

import pandas as pd
import yaml

from btcore.factors import ops
from btcore.factors.expr import evaluate_expr, extract_expr_names, validate_expr

# 缺省因子库：repo 根下顶层 factors/library.yaml（用户可编辑的定义文件）
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "factors" / "library.yaml"

# 因子名保留字：与 bars 必需列 / 引擎派生列 / 伪列冲突的名字禁止登记
_RESERVED_NAMES = frozenset({
    "open", "high", "low", "close", "vol", "amount",
    "adj_factor", "pre_close", "up_limit", "down_limit",
    "open_hfq", "high_hfq", "low_hfq", "close_hfq", "pct_chg",
    "idx_ret", "log_mktcap", "industry",
    "abs", "log",
})


def load_library(path: str | None = None) -> dict[str, dict]:
    """加载并校验因子库，返回 {name: {expr, where?, description?}}。

    加载期对每条 expr 分流校验（纯表达式走 validate_expr，算子表达式走
    validate_op_expr），where 只允许纯表达式；随后对全库做命名引用环检测。
    """
    lib_path = Path(path) if path else _DEFAULT_PATH
    with open(lib_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    factors = (doc or {}).get("factors")
    if not isinstance(factors, dict):
        raise ValueError(f"因子库缺少 factors mapping: {lib_path}")

    for name, spec in factors.items():
        if name in _RESERVED_NAMES:
            raise ValueError(f"因子名 {name!r} 与保留列名冲突: {lib_path}")
        if not isinstance(spec, dict) or "expr" not in spec:
            raise ValueError(f"因子 {name!r} 缺少 expr: {lib_path}")
        try:
            if ops.has_op_call(spec["expr"]):
                ops.validate_op_expr(spec["expr"])
            else:
                validate_expr(spec["expr"])
            if spec.get("where"):
                if ops.has_op_call(spec["where"]):
                    ops.validate_op_expr(spec["where"])
                else:
                    validate_expr(spec["where"])
        except ValueError as exc:
            raise ValueError(f"因子 {name!r} 表达式非法: {exc}") from exc

    _check_cycles(factors)
    return factors


def compute_factor(
    name: str,
    df: pd.DataFrame,
    library: dict | None = None,
) -> pd.Series:
    """按名字计算因子值，返回原始值（不做 rank / 标准化）。

    df 为 MultiIndex (trade_date, symbol) 面板（纯逐行表达式也接受
    当日截面）。命名引用按需递归计算并挂为工作副本的临时列。
    表达式引用 industry / idx_ret / log_mktcap 等伪列时，df 须自行携带。
    """
    lib = library if library is not None else load_library()
    _get_spec(name, lib)
    work = df.copy() if _needs_work_copy(name, df, lib) else df
    return _eval_named(name, work, lib, {})


def compute_factors(
    names: list[str],
    df: pd.DataFrame,
    library: dict | None = None,
) -> pd.DataFrame:
    """批量计算，返回每列一个因子的 DataFrame。

    df 必须包含所有因子依赖的基础列和伪列（industry / log_mktcap / idx_ret）。
    伪列不由此函数附着——调用方需先通过 btcore.engine.ensure_pseudo_columns
    或等价方式准备。纯逐行表达式可接受当日截面；含 ts 算子的表达式需要
    MultiIndex (trade_date, symbol) 面板。
    """
    lib = library if library is not None else load_library()
    work = df.copy()
    memo: dict[str, pd.Series] = {}
    return pd.DataFrame({n: _eval_named(n, work, lib, memo) for n in names})


def resolve_spec(spec: dict, library: dict | None = None) -> dict:
    """把策略 spec 解析成因子引用，供 btcore.strategy_loader 使用。

    输入 {factor: name, weight?, ascending?}，输出 {name, weight, ascending}。
    因子值由引擎物化为列，策略侧不再持有表达式。
    """
    if "expr" in spec:
        raise ValueError(
            "factor_specs 只允许引用因子库名字（factor: name），"
            "不允许直写 expr —— 请先在 factors/library.yaml 登记"
        )
    name = spec.get("factor")
    if not name:
        raise ValueError(f"factor_specs 条目缺少 factor 键: {spec!r}")
    lib = library if library is not None else load_library()
    _get_spec(name, lib)
    return {
        "name": name,
        "weight": float(spec.get("weight", 1.0)),
        "ascending": bool(spec.get("ascending", False)),
        "materialize_only": bool(spec.get("materialize_only", False)),
    }


def resolve_closure(names: list[str], library: dict | None = None) -> dict[str, dict]:
    """解析因子名的传递引用闭包，返回 {name: {expr, where?}}（引擎物化用）。"""
    lib = library if library is not None else load_library()
    closure: dict[str, dict] = {}
    stack = list(names)
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        spec = _get_spec(name, lib)
        closure[name] = spec
        _, refs = spec_names(spec, set(lib))
        stack.extend(refs)
    return closure


# ── 内部 ──


def _get_spec(name: str, library: dict) -> dict:
    if name not in library:
        raise ValueError(f"未知因子 {name!r}，可用: {sorted(library)}")
    return library[name]


def spec_names(spec: dict, factor_names: set[str]) -> tuple[set[str], set[str]]:
    """提取因子定义引用的 (基础列, 因子引用)，expr 与 where 合并统计。"""
    if ops.has_op_call(spec["expr"]):
        cols, refs = ops.extract_op_names(spec["expr"], factor_names)
    else:
        names = extract_expr_names(spec["expr"])
        cols, refs = names - factor_names, names & factor_names
    where = spec.get("where")
    if where:
        if ops.has_op_call(where):
            w_cols, w_refs = ops.extract_op_names(where, factor_names)
            cols |= w_cols
            refs |= w_refs
        else:
            names = extract_expr_names(where)
            cols |= names - factor_names
            refs |= names & factor_names
    return cols, refs


def _check_cycles(factors: dict[str, dict]) -> None:
    """全库命名引用环检测（DFS 三色标记）。"""
    names = set(factors)
    white, gray, black = 0, 1, 2
    color = dict.fromkeys(names, white)

    def visit(name: str, trail: list[str]) -> None:
        color[name] = gray
        _, refs = spec_names(factors[name], names)
        for ref in refs:
            if color[ref] == gray:
                cycle = " -> ".join([*trail, name, ref])
                raise ValueError(f"因子引用存在环: {cycle}")
            if color[ref] == white:
                visit(ref, [*trail, name])
        color[name] = black

    for name in names:
        if color[name] == white:
            visit(name, [])


def _needs_work_copy(name: str, df: pd.DataFrame, lib: dict) -> bool:
    """闭包中有引用未物化为 df 列时，需要工作副本挂临时列。"""
    closure = resolve_closure([name], lib)
    for node_name, spec in closure.items():
        _, refs = spec_names(spec, set(closure))
        if any(ref not in df.columns for ref in refs if ref in closure):
            return True
        if node_name != name and node_name not in df.columns:
            return True
    return False


def _eval_named(
    name: str,
    df: pd.DataFrame,
    lib: dict,
    memo: dict[str, pd.Series],
) -> pd.Series:
    if name in memo:
        return memo[name]
    if name in df.columns:
        memo[name] = df[name]
        return memo[name]
    spec = _get_spec(name, lib)
    _, refs = spec_names(spec, set(lib))
    for ref in refs:
        if ref not in df.columns:
            df[ref] = _eval_named(ref, df, lib, memo)
    memo[name] = _eval_spec(df, spec, name)
    return memo[name]


def _eval_spec(df: pd.DataFrame, spec: dict, name: str) -> pd.Series:
    """对面板求单条因子定义；where 统一为求值后掩码（False → NaN）。"""
    try:
        if ops.has_op_call(spec["expr"]):
            values = ops.eval_op_expr(df, spec["expr"])
        else:
            values = evaluate_expr(df, spec["expr"])
        where = spec.get("where")
        if where:
            if ops.has_op_call(where):
                values = values.where(ops.eval_op_expr(df, where).astype(bool))
            else:
                values = values.where(df.eval(where))
    except Exception as e:
        raise ValueError(f"因子 '{name}' 求值失败: {e}") from e
    return values


def compute_breadth(
    factor_name: str,
    backend,              # DataBackend (needs query_bars and get_calendar)
    start: str,
    end: str,
    library: dict | None = None,
    chunk_days: int = 60,
) -> "pd.Series":
    """流式计算坍缩因子，返回 (trade_date,) 索引的日频 Series。

    仅适用于坍缩算子（mean/group_mean）——这些算子将截面聚合为标量，
    因此可以分块计算后拼接，内存占用 O(chunk_days × N_symbols) 而非
    O(N_dates × N_symbols)。

    Args:
        factor_name: 因子名（必须在 library 中注册且使用坍缩算子）
        backend: DataBackend 实例
        start: 起始日期 YYYYMMDD
        end: 结束日期 YYYYMMDD
        library: 因子库，默认 load_library()
        chunk_days: 每次处理的交易日数，默认 60

    Returns:
        pd.Series with index=trade_date (str), values=日频坍缩标量
    """
    lib = library if library is not None else load_library()
    spec = _get_spec(factor_name, lib)

    kind = ops.collapse_kind(spec["expr"])
    if not kind:
        raise ValueError(
            f"compute_breadth 仅支持坍缩因子，{factor_name!r} 是保形因子"
        )

    # Derive max window from factor DAG
    closure = resolve_closure([factor_name], lib)
    max_window = _max_dag_window(closure)

    calendar = backend.get_calendar(start, end)
    if not len(calendar):
        return pd.Series(dtype=float)

    # Resolve needed columns
    from btcore.factors import plan as factor_plan  # noqa: F811

    all_cols: set[str] = set()
    for node_spec in closure.values():
        cols, _ = spec_names(node_spec, set(closure))
        all_cols |= cols
    base_cols = sorted(all_cols)
    base_cols = factor_plan.expand_columns(base_cols)

    results = []
    i = 0
    while i < len(calendar):
        chunk_end_idx = min(i + chunk_days, len(calendar))
        # Extend lookback for ts window
        lookback_start_idx = max(0, i - max_window)
        chunk_start = calendar[lookback_start_idx]
        chunk_end = calendar[chunk_end_idx - 1]
        actual_start = calendar[i]
        actual_end = calendar[chunk_end_idx - 1]

        # Query bars for this chunk (full market)
        df = backend.query_bars(None, chunk_start, chunk_end, columns=base_cols)
        if df.empty:
            i = chunk_end_idx
            continue

        df.sort_index(inplace=True)
        factor_plan.derive_fields(df)

        # Compute factor for this chunk
        factor_df = compute_factors([factor_name], df, lib)

        # Extract daily scalar (collapse factors: same value for all symbols on a date)
        daily = factor_df.groupby(level="trade_date")[factor_name].first()
        # Filter to actual chunk range (exclude overlap)
        daily = daily.loc[actual_start:actual_end]
        results.append(daily)

        i = chunk_end_idx

    if not results:
        return pd.Series(dtype=float)
    return pd.concat(results)


def _max_dag_window(closure: dict[str, dict]) -> int:
    """从因子闭包计算最大历史窗口（交易日行数）。

    仿 build_factor_plan 的窗口推导逻辑，仅取 max，不关心具体归属。
    """
    names = set(closure)
    order: list[str] = []
    deps: dict[str, set[str]] = {}
    for name, spec in closure.items():
        _, refs = spec_names(spec, names)
        deps[name] = refs

    # Kahn topological sort
    indegree = {n: len(deps[n]) for n in names}
    rdeps: dict[str, set[str]] = {n: set() for n in names}
    for name, refs in deps.items():
        for ref in refs:
            rdeps[ref].add(name)

    queue = sorted(n for n in names if indegree[n] == 0)
    while queue:
        name = queue.pop(0)
        order.append(name)
        for dep in sorted(rdeps[name]):
            indegree[dep] -= 1
            if indegree[dep] == 0:
                queue.append(dep)

    windows: dict[str, int] = {}
    for name in order:
        spec = closure[name]
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
    return max(windows.values(), default=20)
