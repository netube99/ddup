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

import logging
from collections import deque

import numpy as np
import pandas as pd

from btcore.factors import cse, ops
from btcore.factors.expr import extract_expr_names
from btcore.factors.library import eval_spec, spec_names

logger = logging.getLogger(__name__)

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
    """指数参照序列（benchmark hfq_close 的日收益）按日期广播进面板。

    trade_date 必须是 %Y%m%d 字符串（面板契约）：backend 返回 Timestamp 时
    dates.map(ret) 会静默全 NaN，这里 fail-fast 而非产出坏列。
    """
    bench_fn = getattr(backend, "get_benchmark_bars", None)
    if not (callable(bench_fn) and benchmark):
        raise ValueError(
            "因子引用 idx_ret 需要 benchmark 且 backend 提供 get_benchmark_bars"
        )
    dates = df.index.get_level_values("trade_date")
    if not dates.empty and (
        not pd.api.types.is_string_dtype(dates)
        or not (isinstance(dates[0], str) and len(dates[0]) == 8 and dates[0].isdigit())
    ):
        raise ValueError(
            "idx_ret 派生要求 trade_date 为 %Y%m%d 字符串面板，收到 "
            f"{type(dates[0]).__name__}: {dates[0]!r}（backend 日历必须返回字符串日期）"
        )
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
) -> None:
    """按需附着伪列：industry / log_mktcap / idx_ret（原地写列）。

    引擎 preload 与训练侧/研究脚本共用，backend 为鸭子类型（只需有对应方法）。
    idx_ret 默认用 derive_idx_ret 派生。
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
        df["idx_ret"] = derive_idx_ret(df, backend, benchmark)


# 交易日窗口 → 日历天的工程换算（×1.5 + 缓冲）
# 公开：library.compute_breadth 跨模块消费同一换算，避免两份逻辑漂移
#
# 上界（FAC-06）：换算结果供 pd.Timedelta(days=...) 消费，pandas 的
# 可表示上限约 106751 天（int64 纳秒），留余量取 100000；巨窗必须在此
# fail-fast，否则下游抛 OutOfBoundsTimedelta 掩盖真实原因
# （ma(close, 10000000) → 15000010 天）。
MAX_CALENDAR_DAYS = 100_000


def to_calendar_days(trading_rows: int) -> int:
    if trading_rows > MAX_CALENDAR_DAYS:
        # 先挡超大整数，避免 ×1.5 触发 int→float 溢出
        raise ValueError(f"窗口过大: {trading_rows}")
    days = int(trading_rows * 1.5) + 10
    if days > MAX_CALENDAR_DAYS:
        raise ValueError(f"窗口过大: {trading_rows}")
    return days


def build_factor_plan(nodes: dict[str, dict], entry_names: list[str]) -> dict:
    """从因子闭包推导两路供给计划。

    nodes: {name: {expr, where?}}（library.resolve_closure 的输出）；
    entry_names: 策略直接引用的因子名（闭包入口）。

    返回 dict：
      topo:             闭包拓扑序（引用先于被引用方）
      main:             主面板节点集合（非坍缩节点；坍缩节点由投影供值）
      breadth:          需在广度面板计算的节点集合
      collapse:         {坍缩节点: "market"|"group"}（投影方式）
      main_columns:     主面板基础列（已剔除伪列；派生列如 close_hfq 仍在内，
                        请求前需经 expand_columns 展开）
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
    reachable = _ref_closure(nodes, {n for n in entry_names if n in nodes})
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
    breadth = _ref_closure(nodes, set(collapse))
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
    if collapse:
        # 广度面板必须锚定在全市场行情网格上：GenericSQLBackend 的网格是
        # 被请求表的 (trade_date, symbol) 键外并集，纯事件表坍缩表达式
        # （如 pct_sealed 仅引 fd_amount）会让面板退化为事件行 → 分母丢失
        # （2026-09-16 审查 F-FD0-02：0.78 vs 真实 0.01）。锚定一列行情
        # 契约列保证网格=全市场；已有行情列时冗余无害。
        breadth_raw.add("close")
    # log_mktcap 由 total_mv 派生：被引用的面板补请求 total_mv
    if needs["mktcap_main"]:
        main_raw.add("total_mv")
    if needs["mktcap_breadth"]:
        breadth_raw.add("total_mv")

    breadth_days = to_calendar_days(max_window)
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
) -> None:
    """两阶段物化：广度面板求值 + 坍缩投影 + 主面板求值（原地写列）。

    nodes 以 plan["nodes"]（CSE 重写后）为准；CSE 合成节点的临时列在
    物化完成后删除。
    """
    nodes = plan["nodes"]
    breadth_set: set[str] = plan["breadth"]
    if breadth_df is not None:
        for name in plan["topo"]:
            if name in breadth_set:
                breadth_df[name] = eval_spec(breadth_df, nodes[name])
        for name, kind in plan["collapse"].items():
            _project(main_df, breadth_df, name, kind)
    main_set: set[str] = plan["main"]
    for name in plan["topo"]:
        if name in main_set:
            main_df[name] = eval_spec(main_df, nodes[name])
    for tmp in plan.get("cse_temp", ()):
        main_df.drop(columns=tmp, inplace=True, errors="ignore")
        if breadth_df is not None:
            breadth_df.drop(columns=tmp, inplace=True, errors="ignore")


def _nan_day_stats(col: pd.Series) -> dict:
    """按交易日分解坍缩列 NaN 形态（FAC-08 / V-FAC-02）。

    区分三种语义：
      - 前导整日全 NaN（lead_*）：窗口预热区，首个有值日之前，属设计内
      - 整日全 NaN 缺口（gap_*）：首个有值日之后（或无任何有值日）——
        真无值，需要告警
      - 部分行 NaN（partial_*）：同日仍有其他有效值——where 掩码或面板
        网格缺行，不是「整日无值」

    返回 dict：rows/total_rows/all_days/all_rows/lead_days/lead_rows/
    gap_days/gap_rows/gap_dates/partial_rows/partial_days。
    """
    total = len(col)
    mask = col.isna()
    stats = {
        "total_rows": total,
        "rows": int(mask.sum()),
        "all_days": 0, "all_rows": 0,
        "lead_days": 0, "lead_rows": 0,
        "gap_days": 0, "gap_rows": 0, "gap_dates": [],
        "partial_rows": 0, "partial_days": 0,
    }
    if not stats["rows"]:
        return stats

    days = col.index.get_level_values("trade_date")
    by_day = (
        pd.DataFrame({"nan": mask.to_numpy()}, index=days)
        .groupby(level=0)["nan"].agg(["sum", "size"])
    )
    all_nan = by_day["sum"] == by_day["size"]
    stats["all_days"] = int(all_nan.sum())
    stats["all_rows"] = int(by_day.loc[all_nan, "sum"].sum())
    partial = (by_day["sum"] > 0) & ~all_nan
    stats["partial_rows"] = int(by_day.loc[partial, "sum"].sum())
    stats["partial_days"] = int(partial.sum())

    valid = by_day.index[by_day["sum"] < by_day["size"]]
    if len(valid):
        lead = all_nan & (by_day.index < valid[0])
        gap = all_nan & (by_day.index > valid[0])
    else:
        # 全列无值：不当作预热，整体按真异常处理
        lead = pd.Series(False, index=by_day.index)
        gap = all_nan
    stats["lead_days"] = int(lead.sum())
    stats["lead_rows"] = int(by_day.loc[lead, "sum"].sum())
    gap_dates = by_day.index[gap]
    stats["gap_days"] = int(gap.sum())
    stats["gap_rows"] = int(by_day.loc[gap, "sum"].sum())
    stats["gap_dates"] = list(gap_dates)
    return stats


def validate_materialization(
    main_df: pd.DataFrame,
    plan: dict,
) -> list[dict]:
    """物化后验证：检查坍缩因子的完整性和数据质量（唯一验证入口）。

    NaN 语义区分（FAC-08）：
      - 前导整日 NaN = 窗口预热 → info 直接落日志（不进 issues）
      - 首个有值日之后的整日 NaN = 真缺口 → warning issue（占比 > 5%）
      - 部分行 NaN = where 掩码 / 面板网格缺行 → info「掩码缺失」直接落
        日志（不进 issues，避免引擎 issue 通道把 info 当 warning 刷屏）

    info 类发现直接 logger.info，不经 issues 返回——引擎的 issue
    通道白名单只有 warning/error，返回 info 会被降级为 warning 噪声。

    Returns:
        list of dicts with keys: level (warning/error), message
    """
    issues = []

    for name in plan.get("collapse", {}):
        col = main_df.get(name)
        if col is None:
            msg = f"坍缩因子 {name!r} 未物化为主面板列"
            logger.warning(msg)
            issues.append({"level": "warning", "message": msg})
            continue
        st = _nan_day_stats(col)
        if not st["rows"]:
            continue
        if st["lead_days"]:
            msg = (f"坍缩因子 {name!r} 在 {st['lead_days']} 个交易日无值"
                   f"（窗口预热区, {st['lead_rows']} 行 NaN）")
            logger.info(msg)
        if st["gap_days"]:
            pct = st["gap_rows"] / st["total_rows"] if st["total_rows"] else 0.0
            msg = (f"坍缩因子 {name!r} 在 {st['gap_days']} 个交易日无值"
                   f"（{st['gap_rows']}/{st['total_rows']} 行 NaN, "
                   f"占比 {pct:.1%}）")
            if pct > 0.05:
                logger.warning(msg)
                issues.append({"level": "warning", "message": msg})
            else:
                logger.info(msg)
        if st["partial_rows"]:
            msg = (f"坍缩因子 {name!r} 掩码缺失 {st['partial_rows']} 行 NaN"
                   f"（{st['partial_days']} 个交易日部分行有值, 非整日缺失）")
            logger.info(msg)

    return issues


# ── 内部 ──


def _ref_closure(nodes: dict[str, dict], seeds: set[str]) -> set[str]:
    """从种子节点出发的传递引用闭包（含种子自身）。"""
    closure: set[str] = set()
    stack = list(seeds)
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        closure.add(name)
        _, refs = spec_names(nodes[name], set(nodes))
        stack.extend(refs)
    return closure


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
    queue = deque(sorted(n for n in names if indegree[n] == 0))
    order = []
    while queue:
        name = queue.popleft()
        order.append(name)
        for dependent in sorted(rdeps[name]):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                queue.append(dependent)
    if len(order) != len(names):
        raise ValueError(f"因子引用存在环: {sorted(names - set(order))}")
    return order


def _project(
    main_df: pd.DataFrame,
    breadth_df: pd.DataFrame,
    name: str,
    kind: str,
    group_col: str = "industry",
) -> None:
    """坍缩节点从广度面板投影回主面板（原地写列）。

    group_col: 分组坍缩（group_mean 等）的组键列名（F-OP-04：不再硬编码
    industry；组键缺列时 fail-fast，防静默错投影）。
    投影按 (trade_date, symbol) 逐行对齐广度面板值（坍缩算子同日同组同值），
    逐行 reindex 保留 where 后置掩码的 NaN——groupby.first() 会跳过 NaN
    把未掩码值泄漏给已掩码 symbol（docs/factor_library.md §8）。
    """
    if kind != "market" and group_col not in breadth_df.columns:
        raise ValueError(
            f"坍缩因子 {name!r} 分组投影需要组键列 {group_col!r}，"
            "但广度面板无此列——请检查伪列附着需求"
        )
    main_df[name] = breadth_df[name].reindex(main_df.index).to_numpy()
    # FAC-08 / V-FAC-02：只有「首个有值日之后的整日全 NaN」才是真无值；
    # 前导 NaN（窗口预热）与部分行 NaN（where 掩码 / 网格缺行）不报
    # 「N 个交易日无值」——后者交给 validate_materialization 的掩码文案
    st = _nan_day_stats(main_df[name])
    if st["gap_days"]:
        gap_dates = st["gap_dates"]
        logger.warning(
            "坍缩因子 %r 在 %d 个交易日无值: %s ...",
            name, st["gap_days"], gap_dates[:5],
        )
    elif st["rows"]:
        logger.debug(
            "坍缩因子 %r 投影 NaN 属预热/掩码（预热 %d 日, 掩码 %d 行），"
            "不计入无值告警",
            name, st["lead_days"], st["partial_rows"],
        )
