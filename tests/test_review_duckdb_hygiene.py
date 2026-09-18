"""TDD 审查回归 — DuckDB 迁移残留清零与生态利用（F-HYG / F-PERF）。

锁定的机械事实：
1. 源码层（btcore/research/scripts/adapters）无 sqlite3 import、无 SQLite API
   残留；tests/ 豁免（tests/test_generic_sql.py 故意构造旧 SQLite 文件验证
   fail-fast）。rule 14（scripts/check_anticorrupt.py）为防回归门禁。
2. 工作区无 .db 旧库文件（DuckDB 结果库/账本统一 *.duckdb）。
3. pyproject 仅声明 duckdb，uv.lock 锁定的版本落在声明的 >=1.5,<2 区间
   （对齐上游 tushare_db）。
"""

import re
import tomllib
from pathlib import Path

from scripts import check_anticorrupt as ac

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE_LAYERS = ("btcore", "research", "scripts", "adapters")
SKIP_DIRS = {
    ".git", ".venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "node_modules",
}
# 规则定义文件本身必然包含被禁 token 字面量，机械扫描豁免
LINTER_FILE = REPO_ROOT / "scripts" / "check_anticorrupt.py"

_SQLITE_IMPORT_RE = re.compile(r"^\s*(?:import\s+sqlite3\b|from\s+sqlite3\b)", re.MULTILINE)
_SQLITE_API_RE = re.compile(
    r"\b(?:sqlite_master|AUTOINCREMENT|executescript|lastrowid|row_factory)\b"
)


def _source_files(root: Path):
    for path in root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path == LINTER_FILE:
            continue
        yield path


def test_rule14_repo_has_no_sqlite_residue():
    """rule 14：本仓库源码层（linter 自身除外）必须零 sqlite 残留。"""
    errors = ac.check_no_sqlite_residue(str(REPO_ROOT))
    assert errors == [], "\n".join(errors)


def test_no_sqlite_import_or_api_tokens_in_source_layers():
    """独立于 linter 的文本扫描：源码层不得出现 sqlite3 import / SQLite API。"""
    hits: list[str] = []
    for layer in SOURCE_LAYERS:
        layer_root = REPO_ROOT / layer
        if not layer_root.is_dir():
            continue
        for path in _source_files(layer_root):
            rel = path.relative_to(REPO_ROOT)
            text = path.read_text(encoding="utf-8")
            if _SQLITE_IMPORT_RE.search(text):
                hits.append(f"{rel}: sqlite3 import")
            for match in _SQLITE_API_RE.finditer(text):
                hits.append(f"{rel}: {match.group(0)}")
    assert hits == [], "\n".join(hits)


def test_rule14_detects_sqlite_import_variants(tmp_path):
    """rule 14 具备拦截能力：import / from-import 两种形态都要被抓到。"""
    for rel, content in {
        "btcore/database.py": "import sqlite3\n",
        "research/legacy.py": "from sqlite3 import connect\n",
        "adapters/old.py": "",
    }.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    errors = ac.check_no_sqlite_residue(str(tmp_path))
    assert any("sqlite3 import" in e and "btcore/database.py" in e for e in errors)
    assert any("sqlite3 import" in e and "research/legacy.py" in e for e in errors)
    assert not any("adapters/old.py" in e for e in errors)


def test_rule14_detects_sqlite_api_residue(tmp_path):
    """rule 14 拦截 SQLite 专属 API（注释/字符串中出现也算）。"""
    path = tmp_path / "scripts" / "migrate.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "# 旧 schema 曾用 AUTOINCREMENT\n"
        "SQL = \"SELECT name FROM sqlite_master\"\n",
        encoding="utf-8",
    )
    errors = ac.check_no_sqlite_residue(str(tmp_path))
    assert any("AUTOINCREMENT" in e for e in errors)
    assert any("sqlite_master" in e for e in errors)


def test_workspace_has_no_legacy_db_files():
    """旧 SQLite 结果库/账本清理后，工作区不得再残留 *.db 文件。"""
    leftovers = [
        str(path.relative_to(REPO_ROOT))
        for path in REPO_ROOT.rglob("*")
        if path.is_file()
        and path.suffix == ".db"
        and not any(part in SKIP_DIRS for part in path.parts)
    ]
    assert leftovers == []


def test_pyproject_declares_duckdb_only():
    """pyproject 依赖必须含 duckdb>=1.5,<2 且无任何 sqlite 驱动。"""
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        doc = tomllib.load(f)
    deps = doc["project"]["dependencies"]
    duckdb_deps = [d for d in deps if d.lower().startswith("duckdb")]
    assert duckdb_deps, f"缺少 duckdb 依赖: {deps}"
    assert any(">=1.5" in d and "<2" in d for d in duckdb_deps), duckdb_deps
    sqlite_deps = [d for d in deps if "sqlite" in d.lower()]
    assert sqlite_deps == [], sqlite_deps


def test_uv_lock_pins_duckdb_in_declared_range():
    """uv.lock 的 duckdb 锁版必须落在 >=1.5,<2（与上游 tushare_db 对齐）。"""
    lock = (REPO_ROOT / "uv.lock").read_text(encoding="utf-8")
    match = re.search(
        r'\[\[package\]\]\nname = "duckdb"\nversion = "([^"]+)"', lock
    )
    assert match, "uv.lock 未锁定 duckdb 包"
    major, minor = (int(part) for part in match.group(1).split(".")[:2])
    assert (major, minor) >= (1, 5) and major < 2, match.group(1)
