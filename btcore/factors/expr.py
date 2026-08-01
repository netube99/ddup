"""因子表达式机制。

引擎只提供表达式的解析 / 校验 / 求值机制，不内置任何具体因子定义。
策略在配置里声明表达式，引擎对 bar DataFrame 求值。
"""

import ast
import functools

import pandas as pd


def extract_expr_names(expr: str) -> set[str]:
    """提取表达式里的裸标识符名字（AST 扫描）。"""
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression: {expr}") from exc
    return {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}


def validate_expr(expr: str, engine: str = "numexpr") -> None:
    """校验因子表达式。

    安全起见拒绝函数调用 / 属性访问，并用表达式引用的列名构造
    单行 DataFrame 试算，确认表达式可被求值。
    """
    try:
        tree = ast.parse(expr, mode="eval")
    except SyntaxError as exc:
        raise ValueError(f"invalid expression: {expr}") from exc
    for node in ast.walk(tree):
        if isinstance(node, (ast.Call, ast.Attribute)):
            raise ValueError(
                f"factor expressions must not contain function calls or attribute access: {expr}"
            )

    names = extract_expr_names(expr)
    df = pd.DataFrame({n: [1.0] for n in names})
    try:
        df.eval(expr, engine=engine)
    except Exception as exc:
        raise ValueError(f"invalid factor expression (engine={engine}): {expr}") from exc


def evaluate_expr(
    df: pd.DataFrame,
    expr: str,
    where: str | None = None,
    engine: str = "numexpr",
) -> pd.Series:
    """对截面 DataFrame 求表达式值。

    Parameters
    ----------
    df:
        symbol 索引的 DataFrame，含表达式引用的列。
    expr:
        ``pandas.eval`` 支持的算术 / 比较表达式。
    where:
        可选的过滤表达式（仅纯表达式）；与因子库统一口径：求值后掩码
        （False → NaN），不删行、不破坏索引对齐。
    engine:
        传给 ``pandas.eval`` 的求值引擎。

    Returns
    -------
    symbol 索引的表达式值 Series。
    """
    _validate_cached(expr, engine)
    result = df.eval(expr, engine=engine)
    if not isinstance(result, pd.Series):
        result = pd.Series(result, index=df.index)
    if where:
        _validate_cached(where, engine)
        result = result.where(df.eval(where, engine=engine))
    return result


# evaluate_expr 每次调用都 validate 的代价是 AST parse + 单行试算，
# 同一表达式只需验证一次；用 lru_cache 记忆化，无模块级可变状态。
# 非法表达式抛 ValueError 不入缓存，与原行为一致（每次调用都报错）。
@functools.lru_cache(maxsize=None)
def _validate_cached(expr: str, engine: str) -> None:
    validate_expr(expr, engine=engine)
