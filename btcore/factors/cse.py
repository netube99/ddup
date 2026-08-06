"""公共子表达式消除（CSE）— build_factor_plan 前的纯重写优化。

对因子闭包的 expr 做两类重写，物化结果与无 CSE 逐值相等：
1. 完全重复去重：(expr, where) 结构相同的节点重写为对首个同构节点的
   引用（"expr": "<首个节点名>"），只求值一次；
2. 子表达式 CSE，提取为合成节点（__cse_N），各 expr 重写为引用该节点：
   - Call 子树：出现 ≥2 次（跨表达式计数）且不含坍缩算子；
   - 纯算术 BinOp 子树：同表达式内出现 ≥2 次（无 Call/比较/布尔嵌套，
     mf_big_small_div 类连加块——DUP-05 保守口径）。
   合成节点随正常拓扑物化，物化后其临时列由 materialize 删除。

提取是多轮的（DUP-04）：合成节点自身（例如被提取的大子树）的内部公共
子树在下一轮继续提取——"独立出现 + 嵌套在更大被提取子树内"的公共子树
不再被算两次。每轮至少消除一个候选，候选集合有限，必然终止；_MAX_ROUNDS
仅为防呆上限，到达上限静默停止只损失合并机会、不损正确性（仍逐值等价）。

where 子句不参与重写；含坍缩算子的子树不提取（两面板供给语义不同）。
重写产物经 ast.unparse 回到表达式字符串，仍走 validate_op_expr 白名单
校验，不引入新的求值路径。

纯函数模块：仅依赖同包 ops。
"""

import ast
import copy

from btcore.factors import ops

TEMP_PREFIX = "__cse_"

# 多轮重写防呆上限：实际轮数受表达式嵌套深度限制（远小于此值）
_MAX_ROUNDS = 32


def rewrite(nodes: dict[str, dict]) -> dict[str, dict]:
    """返回重写后的 nodes 副本（不修改入参）。合成节点以 TEMP_PREFIX 命名。"""
    nodes = {name: dict(spec) for name, spec in nodes.items()}
    _dedup_identical(nodes)
    for _ in range(_MAX_ROUNDS):
        if not _extract_common_subtrees(nodes):
            break
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


def _is_pure_arith(node: ast.AST) -> bool:
    """纯算术子树：无 Call / Compare / BoolOp 嵌套（仅 Name/Constant/BinOp/一元±）。"""
    for n in ast.walk(node):
        if isinstance(n, (ast.Call, ast.Compare, ast.BoolOp)):
            return False
    return True


class _Rewriter(ast.NodeTransformer):
    """按 mapping 把 Call/BinOp 子树整体替换为 __cse_N 引用。"""

    def __init__(self, mapping: dict[str, str]):
        self.mapping = mapping

    def visit_Call(self, node):
        k = ast.dump(node)
        if k in self.mapping:
            return ast.copy_location(
                ast.Name(id=self.mapping[k], ctx=ast.Load()), node
            )
        return self.generic_visit(node)

    def visit_BinOp(self, node):
        k = ast.dump(node)
        if k in self.mapping:
            return ast.copy_location(
                ast.Name(id=self.mapping[k], ctx=ast.Load()), node
            )
        return self.generic_visit(node)


def _extract_common_subtrees(nodes: dict[str, dict]) -> bool:
    """一轮提取；返回是否发生重写（供 rewrite 多轮循环判断终止）。"""
    trees: dict[str, ast.Expression] = {}
    call_occ: dict[str, list[ast.Call]] = {}               # Call：跨表达式计数
    binop_occ: dict[str, dict[str, list[ast.BinOp]]] = {}  # BinOp：按表达式计数
    for name, spec in nodes.items():
        expr = spec["expr"]
        if not ops.has_op_call(expr):
            continue
        tree = ast.parse(expr, mode="eval")
        trees[name] = tree
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id in ops.OP_NAMES:
                call_occ.setdefault(ast.dump(n), []).append(n)
            elif isinstance(n, ast.BinOp) and _is_pure_arith(n):
                d = ast.dump(n)
                binop_occ.setdefault(name, {}).setdefault(d, []).append(n)

    # 候选：出现次数满足门槛、不含坍缩算子；按收益（子树规模 × 复用次数）降序
    candidates = []
    for k, occ in call_occ.items():
        if len(occ) < 2:
            continue
        sample = occ[0]
        if ops.collapse_kind(ast.unparse(sample)):
            continue
        size = sum(1 for _ in ast.walk(sample))
        candidates.append((size * (len(occ) - 1), k, sample))
    for per_expr in binop_occ.values():
        for k, occ in per_expr.items():
            if len(occ) < 2:
                continue
            size = sum(1 for _ in ast.walk(occ[0]))
            candidates.append((size * (len(occ) - 1), k, occ[0]))
    candidates.sort(key=lambda c: c[0], reverse=True)

    # 选取互不嵌套的候选（被选子树的内部 Call/BinOp 不再单独提取，
    # 嵌套候选留到下一轮——合成节点进入全表后自然可见）
    claimed_calls: set[str] = set()
    claimed_binops: set[str] = set()
    mapping: dict[str, str] = {}
    i = 0
    for _, k, sample in candidates:
        if isinstance(sample, ast.BinOp):
            if k in claimed_binops:
                continue
        elif k in claimed_calls:
            continue
        while f"{TEMP_PREFIX}{i}" in nodes:
            i += 1
        tname = f"{TEMP_PREFIX}{i}"
        i += 1
        # 合成节点体用当前已建映射重写（此时不含 k 自身）：被提取大子树
        # 内部的已提取子公共子树直接引用合成节点，而不是在下一轮被重复
        # 提取/重算（DUP-04："独立出现 + 嵌套在更大被提取子树内"一次算完）。
        # 必须深拷贝：NodeTransformer 的 generic_visit 会原地改写子节点，
        # 直接 visit(sample) 会污染 trees 里的原始节点（外层 dump 失配）
        body = _Rewriter(mapping).visit(copy.deepcopy(sample))
        mapping[k] = tname
        nodes[tname] = {"expr": ast.unparse(ast.fix_missing_locations(body))}
        for n in ast.walk(sample):
            if isinstance(n, ast.Call):
                claimed_calls.add(ast.dump(n))
            elif isinstance(n, ast.BinOp):
                claimed_binops.add(ast.dump(n))

    if not mapping:
        return False

    rewriter = _Rewriter(mapping)
    for name, tree in trees.items():
        new_body = rewriter.visit(tree.body)
        nodes[name]["expr"] = ast.unparse(ast.fix_missing_locations(new_body))
    return True
