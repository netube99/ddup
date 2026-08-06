"""面板算子机制 — 时序(ts) / 截面(xsec) 算子的白名单实现与安全求值器。

所有算子都是 grid→grid 变换：输入输出都对齐 (trade_date, symbol)
MultiIndex 面板，因此 ts 算子、xsec 算子、逐行算术可以在一条表达式里
自由嵌套。坍缩类 xsec 算子（mean/group_mean）在内部立即按日期
广播 / map 回面板网格，"坍缩"只是规划数据供给（全市场广度面板）时的
分类标记，不改变求值语义。

回归类算子（beta/resid_std/corr）用闭式矩展开向量化实现
（rolling 均值的可加性），禁止 rolling_map + Python 回调。

纯函数模块：不依赖 engine / match / database / provider。
"""

import ast
import functools
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

_DATE = "trade_date"
_SYMBOL = "symbol"


# ── ts 族：groupby(symbol) 沿时间轴 ──


def _gshift(s: pd.Series, n: int) -> pd.Series:
    return s.groupby(level=_SYMBOL, sort=False).shift(n)


def _groll(s: pd.Series, n: int, method: str) -> pd.Series:
    r = getattr(s.groupby(level=_SYMBOL, sort=False).rolling(n), method)()
    # groupby 前置分组键且按组排序，droplevel 后恢复原始行序（比较运算要求索引一致）
    return r.droplevel(0).reindex(s.index)


def _ts_delay(x: pd.Series, n: int) -> pd.Series:
    return _gshift(x, n)


def _ts_delta(x: pd.Series, n: int) -> pd.Series:
    return x - _gshift(x, n)


def _ts_roc(x: pd.Series, n: int) -> pd.Series:
    prev = _gshift(x, n)
    return x / prev.where(prev != 0) - 1.0


def _ts_ma(x: pd.Series, n: int) -> pd.Series:
    return _groll(x, n, "mean")


def _ts_ema(x: pd.Series, n: int) -> pd.Series:
    r = x.groupby(level=_SYMBOL, sort=False).ewm(span=n, adjust=False).mean()
    if r.index.nlevels > x.index.nlevels:
        r = r.droplevel(0)
    return r.reindex(x.index)


def _ts_std(x: pd.Series, n: int) -> pd.Series:
    return _groll(x, n, "std")


def _ts_sum(x: pd.Series, n: int) -> pd.Series:
    return _groll(x, n, "sum")


def _ts_max(x: pd.Series, n: int) -> pd.Series:
    return _groll(x, n, "max")


def _ts_min(x: pd.Series, n: int) -> pd.Series:
    return _groll(x, n, "min")


# 闭式 rolling 矩：cov(x,y) = E[xy] - E[x]E[y]，var 同理。
# beta/corr 的分子分母同阶，ddof 口径在比值中抵消；resid_std 为总体口径。
def _roll_mean(s: pd.Series, n: int) -> pd.Series:
    return _groll(s, n, "mean")


def _roll_cov(x: pd.Series, y: pd.Series, n: int) -> pd.Series:
    return _roll_mean(x * y, n) - _roll_mean(x, n) * _roll_mean(y, n)


def _roll_var(x: pd.Series, n: int) -> pd.Series:
    m = _roll_mean(x, n)
    return _roll_mean(x * x, n) - m * m


def _ts_beta(x: pd.Series, y: pd.Series, n: int) -> pd.Series:
    """x 对 y 的 rolling 回归斜率（含截距的一元回归）。"""
    cov = _roll_cov(x, y, n)
    var_y = _roll_var(y, n)
    return cov / var_y.where(var_y != 0)


def _ts_corr(x: pd.Series, y: pd.Series, n: int) -> pd.Series:
    denom = np.sqrt(_roll_var(x, n) * _roll_var(y, n))
    return _roll_cov(x, y, n) / denom.where(denom != 0)


def _ts_resid_std(x: pd.Series, y: pd.Series, n: int) -> pd.Series:
    """x 对 y rolling 回归残差的 rolling 标准差（特质波动率口径）。"""
    cov = _roll_cov(x, y, n)
    var_y = _roll_var(y, n)
    var_x = _roll_var(x, n)
    resid_var = (var_x - cov * cov / var_y.where(var_y != 0)).clip(lower=0.0)
    return np.sqrt(resid_var)


# ── xsec 保形族：groupby(trade_date) 逐日截面，形状不变 ──


def _xs_rank(x: pd.Series) -> pd.Series:
    return x.groupby(level=_DATE, sort=False).rank(pct=True)


def _xs_zscore(x: pd.Series) -> pd.Series:
    g = x.groupby(level=_DATE, sort=False)
    std = g.transform("std")
    return (x - g.transform("mean")) / std.where(std != 0)


def _xs_winsorize(x: pd.Series, p: float) -> pd.Series:
    g = x.groupby(level=_DATE, sort=False)
    lo = g.transform("quantile", p)
    hi = g.transform("quantile", 1.0 - p)
    return x.clip(lower=lo, upper=hi)


def _xs_log(x: pd.Series) -> pd.Series:
    return np.log(x)


def _by_date_and(x: pd.Series, g: pd.Series):
    return x.groupby([x.index.get_level_values(_DATE), g], sort=False)


def _xs_group_rank(x: pd.Series, g: pd.Series) -> pd.Series:
    return _by_date_and(x, g).rank(pct=True)


def _xs_neutralize(x: pd.Series, g: pd.Series, size: pd.Series) -> pd.Series:
    """逐日对 行业哑变量 + size 做 OLS 取残差（行业/市值中性化）。

    向量化实现：按日分块直接构造 numpy 设计矩阵（const + 哑变量 + size），
    消除逐日 get_dummies/concat/DataFrame 重复分配。哑变量列序与缺失语义与
    pd.get_dummies(drop_first=True) 逐位一致（缺失类别不产生列、该行全 0）。
    ``ok.sum() <= mat.shape[1]`` 门槛语义保持（该日整体输出 NaN）。
    """
    gv = g.to_numpy()
    sv = size.to_numpy(dtype=float)
    xv = x.to_numpy(dtype=float)
    out = np.full(len(x), np.nan)
    # 按日分组（sort=False：组按首现序，组内保持原行序）
    for _, pos in x.groupby(level=_DATE, sort=False).indices.items():
        g_d = gv[pos]
        y_d = xv[pos]
        # 当日类别 = 非缺失类别排序；drop_first 丢弃首个类别。
        # 缺失类别行哑变量全 0（与 get_dummies 同语义，不产生 NaN 列）
        valid = ~pd.isna(g_d)
        cats = np.unique(g_d[valid]) if valid.any() else np.empty(0, dtype=object)
        n_dummy = max(len(cats) - 1, 0)
        mat = np.empty((len(pos), n_dummy + 2), dtype=float)
        mat[:, 0] = 1.0
        if n_dummy:
            dummy = np.zeros((len(pos), n_dummy), dtype=float)
            dummy[valid] = (g_d[valid, None] == cats[1:][None, :]).astype(float)
            mat[:, 1:-1] = dummy
        mat[:, -1] = sv[pos]
        ok = ~np.isnan(y_d) & ~np.isnan(mat).any(axis=1)
        if ok.sum() <= mat.shape[1]:
            continue  # 该日样本不足以识别所有系数 → 整体 NaN（门槛语义不变）
        coef, *_ = np.linalg.lstsq(mat[ok], y_d[ok], rcond=None)
        resid = np.full(len(pos), np.nan)
        resid[ok] = y_d[ok] - mat[ok] @ coef
        out[pos] = resid
    return pd.Series(out, index=x.index)


# ── xsec 坍缩族：逐日截面聚合，立即广播/map 回面板网格 ──


def _xs_mean(x: pd.Series) -> pd.Series:
    return x.groupby(level=_DATE, sort=False).transform("mean")


def _xs_group_mean(x: pd.Series, g: pd.Series) -> pd.Series:
    return _by_date_and(x, g).transform("mean")


# ── 算子表（固定白名单，非运行时注册表）──


@dataclass(frozen=True)
class _Op:
    fn: Callable
    n_series: int                       # 前导 Series 参数个数
    n_scalars: int                      # 尾部标量参数个数
    axis: str                           # "ts" | "xsec"
    shape: str                          # "preserve" | "collapse"
    group: bool = False                 # 坍缩是否按分组 key（投影方式不同）
    scalar_kind: str = "window"         # "window"(正整数) | "prob"(0,0.5)
    # 窗口推导：本算子在子表达式窗口基础上额外消耗的历史行数
    window_cost: Callable[[int], int] = lambda n: n


_OPS: dict[str, _Op] = {
    "delay": _Op(_ts_delay, 1, 1, "ts", "preserve",
                 window_cost=lambda n: n),
    "delta": _Op(_ts_delta, 1, 1, "ts", "preserve",
                 window_cost=lambda n: n),
    "roc": _Op(_ts_roc, 1, 1, "ts", "preserve",
               window_cost=lambda n: n),
    "ma": _Op(_ts_ma, 1, 1, "ts", "preserve",
              window_cost=lambda n: n - 1),
    # ema 无限记忆，取 3n 作为工程近似
    "ema": _Op(_ts_ema, 1, 1, "ts", "preserve",
               window_cost=lambda n: 3 * n - 1),
    "std": _Op(_ts_std, 1, 1, "ts", "preserve",
               window_cost=lambda n: n - 1),
    "sum": _Op(_ts_sum, 1, 1, "ts", "preserve",
               window_cost=lambda n: n - 1),
    "max": _Op(_ts_max, 1, 1, "ts", "preserve",
               window_cost=lambda n: n - 1),
    "min": _Op(_ts_min, 1, 1, "ts", "preserve",
               window_cost=lambda n: n - 1),
    "corr": _Op(_ts_corr, 2, 1, "ts", "preserve",
                window_cost=lambda n: n - 1),
    "beta": _Op(_ts_beta, 2, 1, "ts", "preserve",
                window_cost=lambda n: n - 1),
    "resid_std": _Op(_ts_resid_std, 2, 1, "ts", "preserve",
                     window_cost=lambda n: n - 1),
    "abs": _Op(np.abs, 1, 0, "xsec", "preserve"),
    "log": _Op(_xs_log, 1, 0, "xsec", "preserve"),
    "rank": _Op(_xs_rank, 1, 0, "xsec", "preserve"),
    "zscore": _Op(_xs_zscore, 1, 0, "xsec", "preserve"),
    "winsorize": _Op(_xs_winsorize, 1, 1, "xsec", "preserve",
                     scalar_kind="prob"),
    "group_rank": _Op(_xs_group_rank, 2, 0, "xsec", "preserve"),
    "neutralize": _Op(_xs_neutralize, 3, 0, "xsec", "preserve"),
    "mean": _Op(_xs_mean, 1, 0, "xsec", "collapse"),
    "group_mean": _Op(_xs_group_mean, 2, 0, "xsec", "collapse", group=True),
}

# 算子名单一来源：由 _OPS 派生（cse.py / scripts/check_skill_sync.py 消费）
OP_NAMES = frozenset(_OPS)

_BINOPS: dict[type, Callable] = {
    ast.Add: lambda a, b: a + b,
    ast.Sub: lambda a, b: a - b,
    ast.Mult: lambda a, b: a * b,
    ast.Div: lambda a, b: a / b,
    ast.Pow: lambda a, b: a ** b,
    ast.Mod: lambda a, b: a % b,
    ast.FloorDiv: lambda a, b: a // b,
}

_CMPOPS: dict[type, Callable] = {
    ast.Lt: lambda a, b: a < b,
    ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b,
    ast.GtE: lambda a, b: a >= b,
    ast.Eq: lambda a, b: a == b,
    ast.NotEq: lambda a, b: a != b,
}


# ── 表达式结构分析 ──


@functools.lru_cache(maxsize=None)
def _parse(expr: str) -> ast.Expression:
    try:
        return ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression: {expr}") from exc


def has_op_call(expr: str) -> bool:
    """表达式是否含算子调用（决定是否走算子求值器而非 pandas.eval）。"""
    return any(isinstance(n, ast.Call) for n in ast.walk(_parse(expr)))


@functools.lru_cache(maxsize=None)
def validate_op_expr(expr: str) -> None:
    """算子表达式的白名单校验：Call 仅限算子表、禁属性/下标、参数形状检查。

    同一表达式在加载期与每次求值都会校验（CSE 重写产物也经此闸），
    用 lru_cache 记忆化，非法表达式抛 ValueError 不入缓存。
    """
    tree = _parse(expr)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Subscript, ast.Lambda,
                             ast.IfExp, ast.ListComp, ast.DictComp)):
            raise ValueError(f"表达式含禁止的语法: {expr}")
        if isinstance(node, ast.Call):
            if not isinstance(node.func, ast.Name):
                raise ValueError(f"表达式含禁止的语法: {expr}")
            if node.func.id not in _OPS:
                raise ValueError(f"未知算子: {node.func.id} in {expr!r}")
            op = _OPS[node.func.id]
            if node.keywords:
                raise ValueError(f"算子参数必须按位置传: {expr}")
            if len(node.args) != op.n_series + op.n_scalars:
                raise ValueError(
                    f"算子 {node.func.id} 需要 "
                    f"{op.n_series + op.n_scalars} 个参数: {expr}"
                )
            for arg in node.args[op.n_series:]:
                if not isinstance(arg, ast.Constant) or isinstance(arg.value, bool):
                    raise ValueError(f"算子 {node.func.id} 的标量参数必须是数字常量: {expr}")
                if op.scalar_kind == "window":
                    if not isinstance(arg.value, int) or arg.value < 1:
                        raise ValueError(f"窗口参数必须是正整数: {expr}")
                elif not (0 < float(arg.value) < 0.5):
                    raise ValueError(f"winsorize 分位参数必须 ∈ (0, 0.5): {expr}")
        if isinstance(node, ast.Compare) and len(node.ops) > 1:
            raise ValueError(f"不支持链式比较: {expr}")


def extract_op_names(expr: str, factor_names: set[str]) -> tuple[set[str], set[str]]:
    """提取表达式引用的 (基础列, 因子引用)。算子名不出现在结果里。"""
    names = {
        node.id for node in ast.walk(_parse(expr))
        if isinstance(node, ast.Name)
    } - set(_OPS)
    return names - factor_names, names & factor_names


def infer_window(expr: str, ref_windows: dict[str, int]) -> int:
    """推导表达式所需的历史行数（含因子引用的传递窗口）。

    基础列窗口为 1；ts 算子按其 window_cost 在子表达式窗口上累加；
    xsec 算子与逐行运算不消耗时间轴（取子表达式最大值）。
    """
    def _walk(node: ast.AST) -> int:
        if isinstance(node, ast.Expression):
            return _walk(node.body)
        if isinstance(node, ast.Constant):
            return 1
        if isinstance(node, ast.Name):
            return ref_windows.get(node.id, 1)
        if isinstance(node, ast.BinOp):
            return max(_walk(node.left), _walk(node.right))
        if isinstance(node, ast.UnaryOp):
            return _walk(node.operand)
        if isinstance(node, ast.Compare):
            return max(_walk(node.left), _walk(node.comparators[0]))
        if isinstance(node, ast.BoolOp):
            return max(_walk(v) for v in node.values)
        if isinstance(node, ast.Call):
            op = _OPS[node.func.id]
            child = max(_walk(a) for a in node.args[:op.n_series])
            if op.axis == "ts":
                n = int(node.args[-1].value)
                return child + op.window_cost(n)
            return child
        raise ValueError(f"无法推导窗口的表达式: {expr}")

    return _walk(_parse(expr))


def collapse_kind(expr: str) -> str | None:
    """表达式含坍缩算子时返回 "group" / "market"，否则 None（规划用）。"""
    kind = None
    for node in ast.walk(_parse(expr)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            op = _OPS.get(node.func.id)
            if op and op.shape == "collapse":
                if op.group:
                    return "group"
                kind = "market"
    return kind


# ── 求值器 ──


def eval_op_expr(df: pd.DataFrame, expr: str) -> pd.Series:
    """对 (trade_date, symbol) 面板求算子表达式，返回对齐面板索引的 Series。

    裸标识符解析为 df 的列（因子引用由调用方按拓扑序先物化为列）。
    """
    validate_op_expr(expr)

    def _eval(node: ast.AST):
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
                raise ValueError(f"表达式常量必须是数字: {expr}")
            return float(node.value)
        if isinstance(node, ast.Name):
            if node.id not in df.columns:
                raise ValueError(f"未知列或因子引用 {node.id!r}: {expr}")
            return df[node.id]
        if isinstance(node, ast.BinOp):
            fn = _BINOPS.get(type(node.op))
            if fn is None:
                raise ValueError(f"不支持的运算符: {expr}")
            return fn(_eval(node.left), _eval(node.right))
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.USub):
                return -_eval(node.operand)
            if isinstance(node.op, ast.UAdd):
                return _eval(node.operand)
            raise ValueError(f"不支持的一元运算: {expr}")
        if isinstance(node, ast.Compare):
            fn = _CMPOPS.get(type(node.ops[0]))
            if fn is None:
                raise ValueError(f"不支持的比较运算: {expr}")
            return fn(_eval(node.left), _eval(node.comparators[0])).astype(float)
        if isinstance(node, ast.BoolOp):
            values = [(_eval(v) != 0) for v in node.values]
            out = values[0]
            for v in values[1:]:
                out = (out & v) if isinstance(node.op, ast.And) else (out | v)
            return out.astype(float)
        if isinstance(node, ast.Call):
            op = _OPS[node.func.id]
            series_args = [_eval(a) for a in node.args[:op.n_series]]
            scalar_args = [a.value for a in node.args[op.n_series:]]
            return op.fn(*series_args, *scalar_args)
        raise ValueError(f"无法求值的表达式: {expr}")

    result = _eval(_parse(expr))
    if not isinstance(result, pd.Series):
        result = pd.Series(result, index=df.index)
    return result.reindex(df.index)
