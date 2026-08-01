"""Anti-corruption linter for btcore.

Checks that key anti-corruption invariants are not violated:
  1. factors/ must not contain builtin.py
  2. engine.py must not import factors.builtin
  3. Holding must not contain last_adj_factor field
  4. Strategy ABC class attributes must not include behavior switches
  5. factors/ must not expose the old factor-composition API
  6. btcore/ must not import the strategies/ or factors/ or adapters/ layers (one-way dependency)
  7. engine.py must not be imported by btcore internal modules (user-code only)
  8. factors/ layer may only depend on btcore.factors
  9. types.py / constants.py must be zero-dependency (no btcore imports)
  10. match/ submodules (manual / conditions) must not import each other,
      only btcore.match.core
  11. stats.py must stay pure: no sqlite3, no provider/engine imports
  12. btcore/factors must not depend on engine/match/database/provider/backend
  13. btcore module graph must be acyclic (no circular imports)
"""

import ast
import os
import re
import sys
from collections import defaultdict, deque


def check_factors_no_builtin(repo_root: str) -> list[str]:
    errors = []
    builtin_path = os.path.join(repo_root, "btcore", "factors", "builtin.py")
    if os.path.exists(builtin_path):
        errors.append(
            f"VIOLATION: factors/builtin.py must not exist: {builtin_path}"
        )
    return errors


def check_engine_no_builtin_import(repo_root: str) -> list[str]:
    errors = []
    engine_path = os.path.join(repo_root, "btcore", "engine.py")
    if not os.path.exists(engine_path):
        return errors
    # 复用 _iter_import_modules：ast.Import 需遍历 node.names 才能拿到模块名
    for module, lineno in _iter_import_modules(engine_path):
        if "builtin" in module and "factors" in module:
            errors.append(
                f"VIOLATION: engine.py imports factors.builtin: "
                f"line {lineno}"
            )
    return errors


def check_holding_no_last_adj_factor(repo_root: str) -> list[str]:
    errors = []
    types_path = os.path.join(repo_root, "btcore", "types.py")
    if not os.path.exists(types_path):
        return errors
    with open(types_path) as f:
        content = f.read()
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Holding":
            for item in ast.walk(node):
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = item.target.id
                    if name == "last_adj_factor":
                        errors.append(
                            f"VIOLATION: Holding has last_adj_factor field "
                            f"line {item.lineno}"
                        )
    return errors


def check_strategy_no_behavior_switches(repo_root: str) -> list[str]:
    """Strategy ABC must not contain behavior-switch class attributes."""
    behavior_switch_names = {
        "take_profit_mode", "trailing_tp", "trailing_conservative",
        "enable_take_profit", "enable_trailing_tp", "enable_stop_loss",
        "take_profit_pct", "trailing_tp_pct",
    }
    errors = []
    strategy_path = os.path.join(repo_root, "btcore", "strategy.py")
    if not os.path.exists(strategy_path):
        return errors
    with open(strategy_path) as f:
        content = f.read()
    tree = ast.parse(content)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "Strategy":
            for item in ast.walk(node):
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    name = item.target.id
                    if name in behavior_switch_names:
                        errors.append(
                            f"VIOLATION: Strategy ABC has behavior switch '{name}' "
                            f"line {item.lineno}"
                        )
    return errors


def check_factors_no_old_api(repo_root: str) -> list[str]:
    """The old factor-composition layer must not be re-introduced."""
    errors = []
    init_path = os.path.join(repo_root, "btcore", "factors", "__init__.py")
    if not os.path.exists(init_path):
        return errors
    with open(init_path) as f:
        content = f.read()
    forbidden = {
        "StrategyAdapter",
        "FactorSpecItem",
        "FactorPipeline",
        "equal_weight_percentile",
        "CrossSection",
        "Factor",
        "FunctionFactor",
        "register_factor",
        "get_factor_definition",
    }
    for name in forbidden:
        # 词边界匹配，避免 "FactorPipeline" 之类长名被 "Factor" 误报
        if re.search(r"\b" + re.escape(name) + r"\b", content):
            errors.append(
                f"VIOLATION: btcore/factors/__init__.py exposes old API '{name}'"
            )

    factors_dir = os.path.join(repo_root, "btcore", "factors")
    for filename in ("base.py", "adapter.py", "compose.py", "pipeline.py"):
        if os.path.exists(os.path.join(factors_dir, filename)):
            errors.append(
                f"VIOLATION: old factor module still exists: btcore/factors/{filename}"
            )
    return errors


def _iter_import_modules(path: str):
    """Yield imported module strings from a Python file."""
    with open(path) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:  # 跳过相对导入
                yield node.module or "", node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def _iter_py_files(root: str):
    for dirpath, _dirnames, filenames in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for filename in filenames:
            if filename.endswith(".py"):
                yield os.path.join(dirpath, filename)


def check_btcore_no_user_layer_import(repo_root: str) -> list[str]:
    """btcore/ must not import strategies/ factors/ adapters/ user layers (one-way)."""
    errors = []
    forbidden = ("strategies", "factors", "adapters")
    for path in _iter_py_files(os.path.join(repo_root, "btcore")):
        for module, lineno in _iter_import_modules(path):
            if module.split(".")[0] in forbidden:
                errors.append(
                    f"VIOLATION: btcore imports user layer '{module}': "
                    f"{path} line {lineno}"
                )
    return errors


def check_btcore_no_engine_import(repo_root: str) -> list[str]:
    """engine.py must not be imported by btcore internal modules (user-code only)."""
    errors = []
    btcore_dir = os.path.join(repo_root, "btcore")
    for path in _iter_py_files(btcore_dir):
        if os.path.basename(path) == "engine.py":
            continue
        for module, lineno in _iter_import_modules(path):
            if module == "btcore.engine":
                errors.append(
                    f"VIOLATION: btcore internal module imports engine: "
                    f"{path} line {lineno}"
                )
    return errors


def check_factors_layer_deps(repo_root: str) -> list[str]:
    """factors/ layer may only depend on btcore.factors."""
    errors = []
    factors_dir = os.path.join(repo_root, "factors")
    if not os.path.isdir(factors_dir):
        return errors
    forbidden = ("strategies", "research", "adapters", "scripts")
    for path in _iter_py_files(factors_dir):
        for module, lineno in _iter_import_modules(path):
            top = module.split(".")[0]
            if top == "factors":
                continue  # 包内导入
            if top in forbidden:
                errors.append(
                    f"VIOLATION: factors layer imports '{module}': "
                    f"{path} line {lineno}"
                )
            elif top == "btcore" and not module.startswith("btcore.factors"):
                errors.append(
                    f"VIOLATION: factors layer may only use btcore.factors, "
                    f"got '{module}': {path} line {lineno}"
                )
    return errors


def check_types_constants_zero_dep(repo_root: str) -> list[str]:
    """types.py / constants.py must not import anything from btcore (incl. relative)."""
    errors = []
    for filename in ("types.py", "constants.py"):
        path = os.path.join(repo_root, "btcore", filename)
        if not os.path.exists(path):
            continue
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level > 0:
                errors.append(
                    f"VIOLATION: btcore/{filename} has relative import "
                    f"line {node.lineno}"
                )
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = node.module if isinstance(node, ast.ImportFrom) else None
                if mod and mod.split(".")[0] == "btcore":
                    errors.append(
                        f"VIOLATION: btcore/{filename} imports btcore module "
                        f"'{mod}' line {node.lineno}"
                    )
    return errors


def check_match_no_cross_import(repo_root: str) -> list[str]:
    """match/ 子模块（manual/conditions）只允许依赖 core，不得互 import。"""
    errors = []
    match_dir = os.path.join(repo_root, "btcore", "match")
    for filename in ("manual.py", "conditions.py"):
        path = os.path.join(match_dir, filename)
        if not os.path.exists(path):
            continue
        for module, lineno in _iter_import_modules(path):
            if not module.startswith("btcore.match"):
                continue
            if module != "btcore.match.core":
                errors.append(
                    f"VIOLATION: btcore/match/{filename} imports "
                    f"'{module}' (只允许 btcore.match.core) line {lineno}"
                )
    return errors


def check_stats_pure(repo_root: str) -> list[str]:
    """stats.py 纯函数：不得 import sqlite3，不得依赖 provider/engine。"""
    errors = []
    path = os.path.join(repo_root, "btcore", "stats.py")
    if not os.path.exists(path):
        return errors
    for module, lineno in _iter_import_targets(path):
        if module == "sqlite3":
            errors.append(f"VIOLATION: stats.py imports sqlite3 line {lineno}")
        parts = module.split(".")
        if parts[0] == "btcore" and len(parts) > 1 and parts[1] in (
            "provider", "engine", "database", "match", "backend",
        ):
            errors.append(
                f"VIOLATION: stats.py imports infra '{module}' line {lineno}"
            )
    return errors


def _iter_import_targets(path: str):
    """Yield (目标模块, lineno)：同时覆盖 import btcore.x 与 from btcore import x 两种写法。"""
    with open(path) as f:
        tree = ast.parse(f.read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module == "btcore":
                    for alias in node.names:
                        yield f"btcore.{alias.name}", node.lineno
                elif node.module:
                    yield node.module, node.lineno
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield alias.name, node.lineno


def check_factors_no_infra_deps(repo_root: str) -> list[str]:
    """btcore/factors 不得依赖 engine / match / database / provider / backend。"""
    errors = []
    forbidden = ("engine", "match", "database", "provider", "backend")
    factors_dir = os.path.join(repo_root, "btcore", "factors")
    if not os.path.isdir(factors_dir):
        return errors
    for path in _iter_py_files(factors_dir):
        for module, lineno in _iter_import_targets(path):
            parts = module.split(".")
            if parts[0] == "btcore" and len(parts) > 1 and parts[1] in forbidden:
                errors.append(
                    f"VIOLATION: btcore/factors imports infra '{module}': "
                    f"{path} line {lineno}"
                )
    return errors


def check_no_circular_imports(repo_root: str) -> list[str]:
    """btcore 模块级 import 图无环（循环 import 检查）。

    只统计文件体顶层的 import（函数内延迟 import 不算边，避免误报）。
    """
    btcore_dir = os.path.join(repo_root, "btcore")
    # module 名 → 文件路径（跳过 __init__.py，包的聚合导入不算环）
    module_of: dict[str, str] = {}
    for path in _iter_py_files(btcore_dir):
        if os.path.basename(path) == "__init__.py":
            continue
        rel = os.path.relpath(path, repo_root)
        module = rel[:-3].replace(os.sep, ".")
        module_of[module] = path

    adj: dict[str, set[str]] = defaultdict(set)
    for module, path in module_of.items():
        parts = module.split(".")
        with open(path) as f:
            tree = ast.parse(f.read())
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                if node.level == 0:
                    target = node.module or ""
                elif node.level == 1:
                    target = ".".join(parts[:-1] + [node.module or ""])
                elif node.level == 2:
                    target = ".".join(parts[:-2] + [node.module or ""])
                else:
                    continue
                if target in module_of:
                    adj[module].add(target)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in module_of:
                        adj[module].add(alias.name)

    errors = []
    seen = set()
    for start in sorted(adj):
        if start in seen:
            continue
        # BFS 找环：沿边回溯路径
        parent: dict[str, str | None] = {start: None}
        queue = deque([start])
        cycle = None
        while queue:
            cur = queue.popleft()
            for nxt in sorted(adj[cur]):
                if nxt not in parent:
                    parent[nxt] = cur
                    queue.append(nxt)
                elif nxt == start:
                    cycle = cur
                    break
            if cycle:
                break
        if cycle is not None:
            path = [cycle]
            cur = cycle
            while parent[cur] is not None:
                cur = parent[cur]
                path.append(cur)
            path.append(cycle)
            path.reverse()
            errors.append(
                "VIOLATION: circular import: " + " -> ".join(path)
            )
            # 环内节点全部标记，避免重复报告同一环
            for p in path:
                seen.add(p)
        else:
            for p in parent:
                seen.add(p)
    return errors


def main():
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    all_errors = []
    all_errors.extend(check_factors_no_builtin(repo_root))
    all_errors.extend(check_engine_no_builtin_import(repo_root))
    all_errors.extend(check_holding_no_last_adj_factor(repo_root))
    all_errors.extend(check_strategy_no_behavior_switches(repo_root))
    all_errors.extend(check_factors_no_old_api(repo_root))
    all_errors.extend(check_btcore_no_user_layer_import(repo_root))
    all_errors.extend(check_btcore_no_engine_import(repo_root))
    all_errors.extend(check_factors_layer_deps(repo_root))
    all_errors.extend(check_types_constants_zero_dep(repo_root))
    all_errors.extend(check_match_no_cross_import(repo_root))
    all_errors.extend(check_stats_pure(repo_root))
    all_errors.extend(check_factors_no_infra_deps(repo_root))
    all_errors.extend(check_no_circular_imports(repo_root))

    if all_errors:
        print("反腐检查失败:")
        for e in all_errors:
            print(f"  - {e}")
        sys.exit(1)
    print("反腐检查通过 ✓")


if __name__ == "__main__":
    main()
