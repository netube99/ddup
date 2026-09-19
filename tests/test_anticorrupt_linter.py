"""反破坏 linter 测试 — 新补的 5 项检查有真实拦截能力。"""

import subprocess
import sys

from scripts import check_anticorrupt as ac


def test_linter_passes_on_repo():
    """整个仓库当前必须通过全部 15 项检查。"""
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


def test_stats_duckdb_detected(tmp_path):
    _make_tree(tmp_path, {"btcore/stats.py": "import duckdb\n"})
    errs = ac.check_stats_pure(str(tmp_path))
    assert any("duckdb" in e for e in errs)


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


def test_strategies_onnx_import_detected(tmp_path):
    """ML-04：策略层 import onnx/onnxruntime → 机械拦截（文件:行）。"""
    _make_tree(tmp_path, {
        "strategies/x/s.py": "import onnxruntime as ort\n",
        "strategies/y/t.py": "from onnx import load\n",
    })
    errs = ac.check_strategies_no_onnx(str(tmp_path))
    assert any("onnxruntime" in e and "s.py line 1" in e for e in errs)
    assert any("onnx" in e and "t.py line 1" in e for e in errs)


def test_strategies_inference_session_detected(tmp_path):
    """ML-04：策略层调用 InferenceSession（含属性形式）→ 机械拦截。"""
    _make_tree(tmp_path, {
        "strategies/x/s.py": "sess = InferenceSession('m.onnx')\n",
        "strategies/y/t.py": "sess = onnxruntime.InferenceSession('m.onnx')\n",
    })
    errs = ac.check_strategies_no_onnx(str(tmp_path))
    assert sum("InferenceSession" in e for e in errs) == 2


def test_strategies_onnx_compliant_passes(tmp_path):
    """合规反例：策略只用引擎消费通道（bars 列 / bar dict），零违规。"""
    _make_tree(tmp_path, {
        "strategies/x/s.py": (
            "from btcore.strategy import Strategy\n"
            "def select(self, bars, snapshot, provider):\n"
            "    return {\"buy\": [], \"sell\": []}\n"
        ),
        "README.md": "onnx not scanned\n",
    })
    assert ac.check_strategies_no_onnx(str(tmp_path)) == []
