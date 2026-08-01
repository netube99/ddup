#!/usr/bin/env python3
"""校验 .omp/skills/ 中可被代码验证的事实与当前代码一致。

接口变更（CLI flag、算子、YAML 键、协议键、config 默认值）后必须同步 skill；
本脚本发现漂移即非零退出。只对账"skill 提到的事实"，不保证 skill 覆盖完整
（覆盖完整性靠 ddup-docs-drift-audit 流程审计）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".omp" / "skills"
SCRIPTS_DIR = ROOT / "scripts"


def _load_skills() -> dict[str, str]:
    return {p.parent.name: p.read_text(encoding="utf-8") for p in SKILLS_DIR.glob("*/SKILL.md")}


def _section(text: str, header: str) -> str:
    """提取 ## header 到下一个 ## 之间的文本。"""
    m = re.search(re.escape(header) + r"(.*?)(?=\n## |\Z)", text, re.S)
    return m.group(1) if m else ""


def _backticks(text: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text))


def check_cli_flags(skills: dict[str, str], errors: list[str]) -> None:
    """skill 中出现的 --flag 必须存在于某个 scripts/*.py 的 add_argument。"""
    valid: set[str] = set()
    for src in SCRIPTS_DIR.glob("*.py"):
        valid.update(re.findall(r"add_argument\(\s*[\"']--([a-z0-9-]+)[\"']", src.read_text()))
    # skill 中故意提及的不存在 flag（否定式说明），豁免
    allowed_mentions = {"debug"}
    for name, text in skills.items():
        for flag in sorted(set(re.findall(r"(?<![\w-])--([a-z0-9][a-z0-9-]*)", text))):
            if flag not in valid and flag not in allowed_mentions:
                errors.append(f"{name}: --{flag} 不存在于任何 scripts/*.py 的 argparse")


def check_factor_ops(skills: dict[str, str], errors: list[str]) -> None:
    """算子表与 btcore.factors.ops.OP_NAMES 双向一致。"""
    from btcore.factors.ops import OP_NAMES

    section_tokens = _backticks(_section(skills.get("ddup-factor-research", ""), "## 21 算子"))
    phantom = section_tokens - OP_NAMES
    missing = OP_NAMES - section_tokens
    if phantom:
        errors.append(f"ddup-factor-research: 算子表含代码中不存在的算子 {sorted(phantom)}")
    if missing:
        errors.append(f"ddup-factor-research: 代码算子未在 skill 文档化 {sorted(missing)}")


def check_select_keys(skills: dict[str, str], errors: list[str]) -> None:
    """select 返回协议键与 engine._SELECT_KEYS 双向一致。"""
    from btcore.engine import _SELECT_KEYS

    text = skills.get("ddup-strategy-craft", "")
    line = next((ln for ln in text.splitlines() if "合法键仅" in ln), "")
    documented = _backticks(line)
    if set(_SELECT_KEYS) != documented:
        errors.append(
            "ddup-strategy-craft: select 键 "
            f"{sorted(documented)} != engine._SELECT_KEYS {sorted(_SELECT_KEYS)}"
        )


def check_filter_rules(skills: dict[str, str], errors: list[str]) -> None:
    """filter_rules 键与 strategy_loader._KNOWN_FILTER_KEYS 双向一致。"""
    from btcore.strategy_loader import _KNOWN_FILTER_KEYS

    text = skills.get("ddup-strategy-craft", "")
    documented = _backticks(_section(text, "## 6. filter_rules"))
    phantom = documented - _KNOWN_FILTER_KEYS
    missing = _KNOWN_FILTER_KEYS - documented
    if phantom or missing:
        errors.append(
            "ddup-strategy-craft: filter_rules 漂移 "
            f"phantom={sorted(phantom)} missing={sorted(missing)}"
        )


def check_condition_keys(skills: dict[str, str], errors: list[str]) -> None:
    """conditions YAML 键全部在 skill 中出现（代码→skill 单向）。"""
    from btcore.strategy_loader import _CONDITION_KEYS

    all_text = "\n".join(skills.values())
    for key in sorted(_CONDITION_KEYS):
        if key not in all_text:
            errors.append(f"skills: conditions 键 {key} 未在任何 skill 文档化")


def check_ml_contract(skills: dict[str, str], errors: list[str]) -> None:
    """ML meta 版本与账户态特征集与 btcore.ml.spec 一致。"""
    from btcore.ml.spec import META_VERSION, SUPPORTED_STATE_FEATURES

    text = skills.get("ddup-ml-research", "")
    if f"== {META_VERSION}" not in text:
        errors.append(f"ddup-ml-research: 未写明 meta version == {META_VERSION}")
    for feat in sorted(SUPPORTED_STATE_FEATURES):
        if feat not in text:
            errors.append(f"ddup-ml-research: 账户态特征 {feat} 未文档化")


def check_config_defaults(skills: dict[str, str], errors: list[str]) -> None:
    """config 键在 engine/costs 源码中存在；engine 内字面默认值对账（costs 常数值跳过）。"""
    engine_src = (ROOT / "btcore" / "engine.py").read_text()
    costs_src = (ROOT / "btcore" / "costs.py").read_text()
    all_src = engine_src + costs_src
    defaults = {
        m.group(1): m.group(2).strip().strip("\"'").replace("_", "")
        for m in re.finditer(r'config\.get\("(\w+)",\s*([^)]+)\)', engine_src)
    }
    text = skills.get("ddup-strategy-craft", "")
    section = _section(text, "## 4. YAML config 引擎键")
    for key, val in re.findall(r"`(\w+)`=([^\s，、（(]+)", section):
        val = val.strip("\"'")
        if f'"{key}"' not in all_src:
            errors.append(f"ddup-strategy-craft: config 键 {key} 不存在于 engine.py/costs.py")
        elif key in defaults and defaults[key] != val:
            errors.append(
                f"ddup-strategy-craft: config {key} 默认值 skill={val} engine={defaults[key]}"
            )

def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"跳过：{SKILLS_DIR} 不存在")
        return 0
    skills = _load_skills()
    if not skills:
        print("跳过：无项目 skill")
        return 0
    errors: list[str] = []
    check_cli_flags(skills, errors)
    check_factor_ops(skills, errors)
    check_select_keys(skills, errors)
    check_filter_rules(skills, errors)
    check_condition_keys(skills, errors)
    check_ml_contract(skills, errors)
    check_config_defaults(skills, errors)
    if errors:
        print("skill 与代码漂移：")
        for e in errors:
            print(f"  - {e}")
        return 1
    print(f"OK：{len(skills)} 个 skill 与代码事实一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
