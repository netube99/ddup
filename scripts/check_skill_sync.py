#!/usr/bin/env python3
"""Check project skills and canonical documentation against executable facts.

The check is deliberately semantic at the boundaries where words can drift:
operators, select/filter/condition keys, engine defaults, ML metadata, CLI
flags, skill frontmatter, and local Markdown links. It does not try to verify
prose quality or duplicate the full engine test suite.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path
from urllib.parse import unquote

import yaml

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".agents" / "skills"
DOCS_DIR = ROOT / "docs"
SCRIPTS_DIR = ROOT / "scripts"

EXPECTED_SKILLS = {
    "ddup-backtest-analysis",
    "ddup-experiment-design",
    "ddup-factor-research",
    "ddup-live-ops",
    "ddup-ml-research",
    "ddup-research-loop",
    "ddup-review-gate",
    "ddup-strategy-craft",
}


def _load_skills() -> dict[str, str]:
    return {
        path.parent.name: path.read_text(encoding="utf-8")
        for path in sorted(SKILLS_DIR.glob("*/SKILL.md"))
    }


def _between(text: str, start: str, end: str | None = None) -> str:
    start_at = text.find(start)
    if start_at < 0:
        return ""
    start_at += len(start)
    if end is None:
        return text[start_at:]
    end_at = text.find(end, start_at)
    return text[start_at:] if end_at < 0 else text[start_at:end_at]


def _section(text: str, anchor: str, next_anchor: str | None = None) -> str:
    marker = f'<a id="{anchor}"></a>'
    end_marker = f'<a id="{next_anchor}"></a>' if next_anchor else None
    return _between(text, marker, end_marker)


def _first_table_tokens(section: str) -> set[str]:
    for block in re.split(r"\n\s*\n", section):
        if re.search(r"^\|\s*[^|\n]+\s*\|", block, re.M):
            return set(
                re.findall(
                    r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|",
                    block,
                    re.M,
                )
            )
    return set()


def _backticks(text: str) -> set[str]:
    return set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)`", text))


def check_skill_inventory(skills: dict[str, str], errors: list[str]) -> None:
    found = set(skills)
    missing = EXPECTED_SKILLS - found
    extra = found - EXPECTED_SKILLS
    if missing:
        errors.append(f"skills: 缺少任务入口 {sorted(missing)}")
    if extra:
        errors.append(f"skills: 出现未登记任务入口 {sorted(extra)}")

    for name, text in skills.items():
        if not text.startswith("---\n"):
            errors.append(f"{name}: SKILL.md 缺少 YAML frontmatter")
            continue
        try:
            _, frontmatter, _ = text.split("---", 2)
            meta = yaml.safe_load(frontmatter) or {}
        except (ValueError, yaml.YAMLError) as exc:
            errors.append(f"{name}: frontmatter 无法解析: {exc}")
            continue
        if meta.get("name") != name:
            errors.append(f"{name}: frontmatter name={meta.get('name')!r} 与目录不一致")
        description = meta.get("description")
        if not isinstance(description, str) or len(description.strip()) < 20:
            errors.append(f"{name}: description 缺失或过短")


def check_cli_flags(skills: dict[str, str], errors: list[str]) -> None:
    """Require flags named by skills to exist in at least one project CLI."""
    valid: set[str] = set()
    for source in SCRIPTS_DIR.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        valid.update(re.findall(r"[\"']--([a-z0-9][a-z0-9-]*)[\"']", text))
    valid.update({"help", "version"})

    for name, text in skills.items():
        mentioned = set(re.findall(r"(?<![\w-])--([a-z0-9][a-z0-9-]*)", text))
        for flag in sorted(mentioned - valid):
            errors.append(f"{name}: --{flag} 不存在于任何 scripts/*.py 的 argparse")


def check_doc_cli_flags(errors: list[str]) -> None:
    """Check flags used alongside project CLI paths in current documentation."""
    valid: set[str] = set()
    for source in SCRIPTS_DIR.glob("*.py"):
        text = source.read_text(encoding="utf-8")
        valid.update(re.findall(r"[\"']--([a-z0-9][a-z0-9-]*)[\"']", text))
    valid.update({"help", "version"})

    sources = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "ARCHITECTURE.md",
        *(DOCS_DIR / name for name in ("backend_guide.md", "factor_library.md",
         "factor_research.md", "strategy_guide.md", "strategy_patterns.md",
         "cli_and_research.md", "results_analysis.md", "ml_guide.md",
         "live_guide.md", "development.md", "audit_checklist.md",
         "review_protocol.md")),
    ]
    for source in sources:
        text = source.read_text(encoding="utf-8").replace("\\\n", " ")
        for line in text.splitlines():
            if "scripts/" not in line:
                continue
            for flag in set(re.findall(r"(?<![\w-])--([a-z0-9][a-z0-9-]*)", line)):
                if flag not in valid:
                    errors.append(
                        f"{source.relative_to(ROOT)}: --{flag} 不存在于项目 CLI argparse"
                    )


def check_factor_ops(errors: list[str]) -> None:
    from btcore.factors.ops import OP_NAMES

    text = (DOCS_DIR / "factor_library.md").read_text(encoding="utf-8")
    documented = _first_table_tokens(_section(text, "operators"))
    phantom = documented - OP_NAMES
    missing = set(OP_NAMES) - documented
    if phantom:
        errors.append(f"factor_library: 包含不存在的算子 {sorted(phantom)}")
    if missing:
        errors.append(f"factor_library: 未记录代码算子 {sorted(missing)}")


def check_select_keys(errors: list[str]) -> None:
    from btcore.engine import _SELECT_KEYS

    text = (DOCS_DIR / "strategy_guide.md").read_text(encoding="utf-8")
    documented = _first_table_tokens(
        _section(text, "select").split("### `calc_conditions", 1)[0]
    )
    if documented != set(_SELECT_KEYS):
        errors.append(
            "strategy_guide select: "
            f"{sorted(documented)} != engine._SELECT_KEYS {sorted(_SELECT_KEYS)}"
        )


def check_filter_rules(errors: list[str]) -> None:
    from btcore.strategy_loader import _KNOWN_FILTER_KEYS

    text = (DOCS_DIR / "strategy_guide.md").read_text(encoding="utf-8")
    documented = _first_table_tokens(_section(text, "filters", "condition-config"))
    phantom = documented - _KNOWN_FILTER_KEYS
    missing = set(_KNOWN_FILTER_KEYS) - documented
    if phantom or missing:
        errors.append(
            "strategy_guide filter_rules: "
            f"phantom={sorted(phantom)} missing={sorted(missing)}"
        )


def check_condition_keys(errors: list[str]) -> None:
    from btcore.strategy_loader import _CONDITION_KEYS

    text = (DOCS_DIR / "strategy_guide.md").read_text(encoding="utf-8")
    section = _section(text, "condition-config", "conditions")
    documented = _backticks(section)
    documented.update(re.findall(r"^\s*([a-z_]+):", section, re.M))
    missing = set(_CONDITION_KEYS) - documented
    if missing:
        errors.append(f"strategy_guide conditions: 未记录 {sorted(missing)}")


def _engine_defaults() -> dict[str, object]:
    """Extract literal config.get defaults without importing the engine."""
    source = (ROOT / "btcore" / "engine.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    defaults: dict[str, object] = {}
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "get"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "config"
            and len(node.args) >= 2
        ):
            continue
        key = node.args[0]
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            continue
        try:
            defaults[key.value] = ast.literal_eval(node.args[1])
        except (ValueError, TypeError):
            continue

    from btcore.constants import (
        COMMISSION_RATE,
        MIN_COMMISSION,
        STAMP_TAX_RATE,
        TRANSFER_FEE_RATE,
    )

    defaults.update(
        commission_rate=COMMISSION_RATE,
        min_commission=MIN_COMMISSION,
        stamp_tax_rate=STAMP_TAX_RATE,
        transfer_fee_rate=TRANSFER_FEE_RATE,
    )
    return defaults


def _parse_literal(token: str) -> object:
    value = token.strip().strip("`")
    if value in {"", "—", "自动推导"}:
        return None
    try:
        return ast.literal_eval(value)
    except (ValueError, TypeError, SyntaxError):
        try:
            return float(value)
        except ValueError:
            return value


def check_config_defaults(errors: list[str]) -> None:
    text = (DOCS_DIR / "strategy_guide.md").read_text(encoding="utf-8")
    section = _section(text, "config", "factors")
    rows = re.findall(
        r"^\|\s*`([A-Za-z_][A-Za-z0-9_]*)`\s*\|\s*[^|]+\|\s*([^|]+?)\s*\|",
        section,
        re.M,
    )
    documented = {key: _parse_literal(value) for key, value in rows}
    for key, expected in sorted(_engine_defaults().items()):
        if key not in documented:
            errors.append(f"strategy_guide config: 未记录引擎键 {key}")
            continue
        actual = documented[key]
        if actual is None:
            continue
        if actual != expected:
            errors.append(
                f"strategy_guide config {key}: doc={actual!r} engine={expected!r}"
            )


def check_ml_contract(errors: list[str]) -> None:
    from btcore.ml.spec import META_VERSION, SUPPORTED_STATE_FEATURES

    text = (DOCS_DIR / "ml_guide.md").read_text(encoding="utf-8")
    if f"META_VERSION == {META_VERSION}" not in text:
        errors.append(f"ml_guide: 未写明 META_VERSION == {META_VERSION}")
    for version in set(re.findall(r"META_VERSION == (\d+)", text)) - {
        str(META_VERSION)
    }:
        errors.append(f"ml_guide: META_VERSION == {version} 与代码 {META_VERSION} 冲突")
    for feature in sorted(SUPPORTED_STATE_FEATURES):
        if feature not in text:
            errors.append(f"ml_guide: 未记录账户态特征 {feature}")


def check_factor_arithmetic(errors: list[str]) -> None:
    text = (DOCS_DIR / "factor_library.md").read_text(encoding="utf-8")
    section = _section(text, "boolean-score", "operators")
    if "* 1" not in section:
        errors.append("factor_library: 布尔计分示例未显式使用 `* 1`")
    if "布尔值可直接相加" in section:
        errors.append("factor_library: 仍宣称布尔值可直接相加")


def _slugify(heading: str) -> str:
    heading = re.sub(r"<[^>]+>", "", heading)
    heading = re.sub(r"[`*~]", "", heading).strip().lower()
    out: list[str] = []
    for char in heading:
        if char.isalnum() or char in "-_":
            out.append(char)
        elif char.isspace():
            out.append("-")
    return re.sub(r"-+", "-", "".join(out)).strip("-")


def _without_fences(text: str) -> str:
    return re.sub(r"```.*?```", "", text, flags=re.S)


def _markdown_anchors(text: str) -> set[str]:
    text = _without_fences(text)
    anchors = set(re.findall(r'<a\s+id=["\']([^"\']+)["\']', text))
    anchors.update(_slugify(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+)$", text, re.M))
    return anchors


def check_local_links(errors: list[str]) -> None:
    markdown_files = [
        ROOT / "README.md",
        ROOT / "AGENTS.md",
        ROOT / "ARCHITECTURE.md",
        *sorted(DOCS_DIR.rglob("*.md")),
        *sorted(SKILLS_DIR.glob("*/SKILL.md")),
    ]
    anchor_cache: dict[Path, set[str]] = {}

    for source in (path for path in markdown_files if path.is_file()):
        text = _without_fences(source.read_text(encoding="utf-8"))
        for raw_target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
            target = raw_target.strip()
            if target.startswith("<") and target.endswith(">"):
                target = target[1:-1]
            if not target or target.startswith(("http://", "https://", "mailto:")):
                continue
            target = unquote(target)
            path_part, _, fragment = target.partition("#")
            destination = source if not path_part else (source.parent / path_part).resolve()
            try:
                destination.relative_to(ROOT)
            except ValueError:
                errors.append(f"{source.relative_to(ROOT)}: 链接越出项目 {raw_target}")
                continue
            if not destination.exists():
                errors.append(f"{source.relative_to(ROOT)}: 链接目标不存在 {raw_target}")
                continue
            if fragment and destination.suffix == ".md":
                if destination not in anchor_cache:
                    anchor_cache[destination] = _markdown_anchors(
                        destination.read_text(encoding="utf-8")
                    )
                if fragment not in anchor_cache[destination]:
                    errors.append(
                        f"{source.relative_to(ROOT)}: 目标缺少锚点 #{fragment} ({raw_target})"
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
    check_skill_inventory(skills, errors)
    check_cli_flags(skills, errors)
    check_doc_cli_flags(errors)
    check_factor_ops(errors)
    check_select_keys(errors)
    check_filter_rules(errors)
    check_condition_keys(errors)
    check_config_defaults(errors)
    check_ml_contract(errors)
    check_factor_arithmetic(errors)
    check_local_links(errors)

    if errors:
        print("文档、skill 与代码漂移：")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"OK：{len(skills)} 个 skill、代码契约与本地链接一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
