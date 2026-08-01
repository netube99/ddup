"""反破坏 linter 测试 — 新补的 5 项检查有真实拦截能力。"""

import subprocess
import sys

from scripts import check_anticorrupt as ac


def test_linter_passes_on_repo():
    """整个仓库当前必须通过全部 13 项检查。"""
    r = subprocess.run(
        [sys.executable, "scripts/check_anticorrupt.py"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stdout + r.stderr


def _make_tree(tmp_path, files: dict[str, str]):
    for rel, content in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_types_zero_dep_detected(tmp_path):
    _make_tree(tmp_path, {
        "btcore/types.py": "from btcore.constants import X\n",
        "btcore/constants.py": "",
    })
    errs = ac.check_types_constants_zero_dep(str(tmp_path))
    assert any("types.py" in e for e in errs)


def test_match_cross_import_detected(tmp_path):
    _make_tree(tmp_path, {
        "btcore/match/core.py": "",
        "btcore/match/manual.py": "from btcore.match.conditions import x\n",
        "btcore/match/conditions.py": "from btcore.match.core import y\n",
    })
    errs = ac.check_match_no_cross_import(str(tmp_path))
    assert any("manual.py" in e for e in errs)


def test_stats_sqlite_detected(tmp_path):
    _make_tree(tmp_path, {"btcore/stats.py": "import sqlite3\n"})
    errs = ac.check_stats_pure(str(tmp_path))
    assert any("sqlite3" in e for e in errs)


def test_factors_infra_dep_detected(tmp_path):
    _make_tree(tmp_path, {
        "btcore/factors/ops.py": "from btcore import database\n",
    })
    errs = ac.check_factors_no_infra_deps(str(tmp_path))
    assert any("database" in e for e in errs)


def test_circular_import_detected(tmp_path):
    _make_tree(tmp_path, {
        "btcore/a.py": "import btcore.b\n",
        "btcore/b.py": "import btcore.a\n",
    })
    errs = ac.check_no_circular_imports(str(tmp_path))
    assert any("circular" in e for e in errs)
    # 环方向从 BFS 起点而定，只断言节点集合
    assert "btcore.a" in errs[0] and "btcore.b" in errs[0]
