"""公共子表达式消除（CSE）— build_factor_plan 前的纯重写优化。

对因子闭包的 expr 做两类重写，物化结果与无 CSE 逐值相等：
1. 完全重复去重：(expr, where) 结构相同的节点重写为对首个同构节点的
   引用（"expr": "<首个节点名>"），只求值一次；
2. 子表达式 CSE：出现 ≥2 次且不含坍缩算子的 Call 子树提取为合成节点
   （__cse_N），各 expr 重写为引用该节点，合成节点随正常拓扑物化，
   物化后其临时列由 materialize 删除。

where 子句不参与重写；含坍缩算子的子树不提取（两面板供给语义不同）。
重写产物经 ast.unparse 回到表达式字符串，仍走 validate_op_expr 白名单
校验，不引入新的求值路径。

纯函数模块：仅依赖同包 ops。
"""

import ast

from btcore.factors import ops

TEMP_PREFIX = "__cse_"


def rewrite(nodes: dict[str, dict]) -> dict[str, dict]:
    """返回重写后的 nodes 副本（不修改入参）。合成节点以 TEMP_PREFIX 命名。"""
    nodes = {name: dict(spec) for name, spec in nodes.items()}
    _dedup_identical(nodes)
    _extract_common_calls(nodes)
    return nodes


def _dump(expr: str) -> str | None:
    try:
        return ast.dump(ast.parse(expr, mode="eval"))
    except SyntaxError:
        return None  # 非法表达式留给后续校验报错


def _dedup_identical(nodes: dict[str, dict]) -> None:
    seen: dict[tuple[str, str], str] = {}
    for name, spec in nodes.items():
        k = _dump(spec["expr"])
        if k is None:
            continue
        key = (k, spec.get("where") or "")
        if key in seen:
            spec["expr"] = seen[key]
            spec.pop("where", None)
        else:
            seen[key] = name


def _extract_common_calls(nodes: dict[str, dict]) -> None:
    trees: dict[str, ast.Expression] = {}
    occurrences: dict[str, list[ast.Call]] = {}
    for name, spec in nodes.items():
        expr = spec["expr"]
        if not ops.has_op_call(expr):
            continue
        tree = ast.parse(expr, mode="eval")
        trees[name] = tree
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in ops.OP_NAMES:
                occurrences.setdefault(ast.dump(n), []).append(n)

    # 候选：出现 ≥2 次、不含坍缩算子；按收益（子树规模 × 复用次数）降序
    candidates = []
    for k, occ in occurrences.items():
        if len(occ) < 2:
            continue
        sample = occ[0]
        if ops.collapse_kind(ast.unparse(sample)):
            continue
        size = sum(1 for _ in ast.walk(sample))
        candidates.append((size * (len(occ) - 1), k, sample))
    candidates.sort(key=lambda c: c[0], reverse=True)

    # 选取互不嵌套的候选（被选子树的内部子树不再单独提取）
    claimed: set[str] = set()
    mapping: dict[str, str] = {}
    i = 0
    for _, k, sample in candidates:
        if k in claimed:
            continue
        while f"{TEMP_PREFIX}{i}" in nodes:
            i += 1
        tname = f"{TEMP_PREFIX}{i}"
        i += 1
        mapping[k] = tname
        nodes[tname] = {"expr": ast.unparse(sample)}
        for n in ast.walk(sample):
            if isinstance(n, ast.Call):
                claimed.add(ast.dump(n))

    if not mapping:
        return

    class _Rewriter(ast.NodeTransformer):
        def visit_Call(self, node):
            k = ast.dump(node)
            if k in mapping:
                return ast.copy_location(
                    ast.Name(id=mapping[k], ctx=ast.Load()), node
                )
            return self.generic_visit(node)

    for name, tree in trees.items():
        new_body = _Rewriter().visit(tree.body)
        nodes[name]["expr"] = ast.unparse(ast.fix_missing_locations(new_body))
