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


class _UniqueKeyLoader(yaml.SafeLoader):
    """重复键 fail-fast：PyYAML 默认后者静默覆盖，因子库手工维护易踩。"""

    def construct_mapping(self, node, deep=False):
        mapping = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                line = key_node.start_mark.line + 1
                raise ValueError(f"因子库重复键 {key!r}（line {line}）")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping

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
        doc = yaml.load(f, Loader=_UniqueKeyLoader)
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


def compute_factors(
    names: list[str],
    df: pd.DataFrame,
    library: dict | None = None,
) -> pd.DataFrame:
    """批量计算，返回每列一个因子的 DataFrame。

    df 必须包含所有因子依赖的基础列和伪列（industry / log_mktcap / idx_ret）。
    伪列不由此函数附着——调用方需先通过 btcore.factors.plan.ensure_pseudo_columns
    或等价方式准备。纯逐行表达式可接受当日截面；含 ts 算子的表达式需要
    MultiIndex (trade_date, symbol) 面板。
    """
    lib = library if library is not None else load_library()
    work = df.copy()
    memo: dict[str, pd.Series] = {}
    result = {}
    for n in names:
        try:
            result[n] = _eval_named(n, work, lib, memo)
        except ValueError as e:
            raise ValueError(
                f"计算入口因子 '{n}' 时出错:\n  {e}"
            ) from e
    return pd.DataFrame(result)


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
    weight = spec.get("weight", 1.0)
    if isinstance(weight, bool) or not isinstance(weight, (int, float)) \
            or weight != weight:
        raise ValueError(f"factor_specs 条目 weight 必须是有限数值: {weight!r}")
    ascending = spec.get("ascending", False)
    if not isinstance(ascending, bool):
        raise ValueError(f"factor_specs 条目 ascending 必须是 bool: {ascending!r}")
    materialize_only = spec.get("materialize_only", False)
    if not isinstance(materialize_only, bool):
        raise ValueError(
            f"factor_specs 条目 materialize_only 必须是 bool: {materialize_only!r}"
        )
    return {
        "name": name,
        "weight": weight,
        "ascending": ascending,
        "materialize_only": materialize_only,
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
            try:
                df[ref] = _eval_named(ref, df, lib, memo)
            except ValueError as e:
                raise ValueError(
                    f"计算因子 '{name}' 时依赖因子 '{ref}' 求值失败:\n  {e}"
                ) from e
    memo[name] = _eval_spec(df, spec, name)
    return memo[name]


def _eval_spec(df: pd.DataFrame, spec: dict, name: str) -> pd.Series:
    """对面板求单条因子定义；where 统一为求值后掩码（False → NaN）。"""
    try:
        if ops.has_op_call(spec["expr"]):
            values = ops.eval_op_expr(df, spec["expr"])
            where = spec.get("where")
            if where:
                values = values.where(ops.eval_op_expr(df, where).astype(bool))
        else:
            values = evaluate_expr(df, spec["expr"], where=spec.get("where"))
    except Exception as e:
        missing = _detect_missing_columns(df, spec)
        detail = f"因子 '{name}' 求值失败: {e}"
        if missing:
            detail += f"\n  缺少列: {sorted(missing)}（不在数据面板中）"
        raise ValueError(detail) from e
    return values


def _detect_missing_columns(df: pd.DataFrame, spec: dict) -> set[str]:
    """检测因子定义的 expr/where 引用了哪些不在 df 中的列名。"""
    missing: set[str] = set()
    for source in ["expr", "where"]:
        text = spec.get(source)
        if not text:
            continue
        if ops.has_op_call(text):
            cols, _ = ops.extract_op_names(text, set())
        else:
            cols = extract_expr_names(text) - set()
        missing |= {c for c in cols if c not in df.columns}
    return missing


def compute_breadth(
    factor_name: str,
    backend,              # DataBackend (needs query_bars and get_calendar)
    lib: dict,
    start: str,
    end: str,
    *,
    benchmark: str | None = None,
    chunk_days: int = 60,
) -> "pd.Series":
    """流式计算坍缩因子，返回 (trade_date,) 索引的日频 Series。

    仅适用于坍缩算子（mean/group_mean）——这些算子将截面聚合为标量，
    因此可以分块计算后拼接，内存占用 O(chunk_days × N_symbols) 而非
    O(N_dates × N_symbols)。

    列推导 / 伪列需求 / warmup 窗口全部由 build_factor_plan 产物供给，
    与引擎 preload 同一推导路径（无第二份手搓逻辑）。

    Args:
        factor_name: 因子名（必须在 lib 中注册且使用坍缩算子）
        backend: DataBackend 实例
        lib: 因子库（load_library() 产物）
        start: 起始日期 YYYYMMDD
        end: 结束日期 YYYYMMDD
        benchmark: 基准代码（闭包引用 idx_ret 时必需，透传 ensure_pseudo_columns）
        chunk_days: 每次处理的交易日数，默认 60

    Returns:
        pd.Series with index=trade_date (str), values=日频坍缩标量
    """
    spec = _get_spec(factor_name, lib)
    # 局部 import 避免模块级循环（plan 模块级依赖 library.spec_names）
    from btcore.factors import plan as factor_plan

    kind = ops.collapse_kind(spec["expr"])
    if not kind:
        raise ValueError(
            f"compute_breadth 仅支持坍缩因子，{factor_name!r} 是保形因子"
        )

    # 供给计划与引擎 preload 同源：窗口 / 列 / 伪列需求都取 plan 产物
    closure = resolve_closure([factor_name], lib)
    fplan = factor_plan.build_factor_plan(closure, [factor_name])
    max_window = max(fplan["windows"].values(), default=1)

    calendar = backend.get_calendar(start, end)
    if not len(calendar):
        return pd.Series(dtype=float)

    # warmup 前伸：请求起点前多取 max_window 个交易日，物化后裁剪回请求
    # 区间——否则区间前段 ts 窗口不足会静默算错（口径同引擎 preload）
    cal_start = (
        pd.Timestamp(start)
        - pd.Timedelta(days=factor_plan.to_calendar_days(max_window))
    ).strftime("%Y%m%d")
    calendar_all = backend.get_calendar(cal_start, end)
    if not len(calendar_all):
        return pd.Series(dtype=float)
    request_days = [d for d in calendar_all if d >= start]
    lookback = [d for d in calendar_all if d < start]
    if not request_days:
        return pd.Series(dtype=float)
    full_cal = lookback + request_days
    offset = len(lookback)

    # 单面板（全市场）计算整闭包：请求列取主/广度两侧并集再展开
    # （log_mktcap → total_mv 补列由 plan 的 mktcap 逻辑保证）
    base_cols = factor_plan.expand_columns(
        fplan["main_columns"] | fplan["breadth_columns"]
    )

    results = []
    i = 0
    n = len(request_days)
    while i < n:
        chunk_end_idx = min(i + chunk_days, n)
        # 分块起点前伸：在 lookback+request 拼接日历上回溯窗口
        lookback_start_idx = max(0, offset + i - max_window)
        chunk_start = full_cal[lookback_start_idx]
        chunk_end = request_days[chunk_end_idx - 1]
        actual_start = request_days[i]
        actual_end = request_days[chunk_end_idx - 1]

        # Query bars for this chunk (full market)
        df = backend.query_bars(None, chunk_start, chunk_end, columns=base_cols)
        if df.empty:
            i = chunk_end_idx
            continue

        df.sort_index(inplace=True)
        factor_plan.derive_fields(df)
        # 伪列附着：group_mean 引用 industry、idx_ret、log_mktcap 时必须先附着
        # （2026-08-03 实证 F-BRD-02：此前从未附着，industry_mom 直接 ValueError）。
        # 单面板取 main 侧：主/广度两侧需求归并到 main 键
        pneeds = fplan["needs"]
        needs = {
            "industry_main": pneeds["industry_main"] or pneeds["industry_breadth"],
            "mktcap_main": pneeds["mktcap_main"] or pneeds["mktcap_breadth"],
            "index": pneeds["index"],
        }
        if any(needs.values()):
            factor_plan.ensure_pseudo_columns(
                df, needs, "main", backend=backend, benchmark=benchmark
            )

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
