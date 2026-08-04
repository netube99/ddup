"""sweep 工具函数测试。"""

import subprocess
import sys

import yaml

from research.sweep import expand_params, nested_set


def test_nested_set_existing_key():
    """nested_set 修改嵌套 dict 中已存在的键。"""
    d = {"a": {"b": 1}}
    nested_set(d, "a.b", 42)
    assert d["a"]["b"] == 42


def test_nested_set_new_key():
    """nested_set 在嵌套 dict 中创建不存在的路径。"""
    d = {"a": {}}
    nested_set(d, "a.b.c", "hello")
    assert d["a"]["b"]["c"] == "hello"


def test_nested_set_top_level():
    """nested_set 设置顶层键。"""
    d = {"x": 1}
    nested_set(d, "y", 2)
    assert d["y"] == 2
    assert d["x"] == 1


def test_nested_set_float_value():
    """nested_set 设置浮点值。"""
    d = {"config": {}}
    nested_set(d, "config.threshold", 0.03)
    assert d["config"]["threshold"] == 0.03


def test_expand_params_single():
    """expand_params 单参数展开。"""
    params = {"top_k": [5, 10]}
    result = expand_params(params)
    assert len(result) == 2
    labels = [r[0] for r in result]
    assert "top_k=5" in labels
    assert "top_k=10" in labels


def test_expand_params_multi():
    """expand_params 多参数笛卡尔积。"""
    params = {"top_k": [5], "threshold": [0.02, 0.03]}
    result = expand_params(params)
    assert len(result) == 2
    labels = [r[0] for r in result]
    assert "top_k=5, threshold=0.02" in labels
    assert "top_k=5, threshold=0.03" in labels


def test_expand_params_nested_keys():
    """expand_params 嵌套参数路径的标签使用最后一段。"""
    params = {"config.max_positions": [10, 20]}
    result = expand_params(params)
    assert len(result) == 2
    labels = [r[0] for r in result]
    assert "max_positions=10" in labels
    assert "max_positions=20" in labels


def test_expand_params_dict_values():
    """expand_params 验证返回的 param_dict 结构。"""
    params = {"top_k": [3], "config.max_positions": [5]}
    result = expand_params(params)
    assert len(result) == 1
    label, param_dict = result[0]
    assert param_dict == {"top_k": 3, "config.max_positions": 5}
    assert "top_k=3" in label
    assert "max_positions=5" in label


def test_dry_run_flag(tmp_path):
    """--dry-run 输出参数组合但不运行回测。"""

    # 创建临时 sweep config
    sweep_config = tmp_path / "sweep.yaml"
    base_config = tmp_path / "base.yaml"
    base_config.write_text("top_k: 5\n")

    sweep_config.write_text(
        yaml.dump({"base": str(base_config), "params": {"top_k": [5, 10]}})
    )

    result = subprocess.run(
        [sys.executable, "scripts/sweep.py", str(sweep_config),
         "--start", "20240101", "--end", "20240131", "--dry-run"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert "参数组合数: 2" in result.stdout
    assert "top_k=5" in result.stdout
    assert "top_k=10" in result.stdout


def test_run_loop_writes_standard_runs(tmp_path, monkeypatch):
    """每组参数作为标准 run 写入同一输出库（compare.py 可读）+ --no-report。"""
    import json
    import sqlite3

    import scripts.sweep as sweep_mod

    sweep_config = tmp_path / "sweep.yaml"
    base_config = tmp_path / "base.yaml"
    base_config.write_text("top_k: 5\n")
    sweep_config.write_text(
        yaml.dump({"base": str(base_config), "params": {"top_k": [5, 10]}})
    )
    out_db = tmp_path / "sweep.db"

    captured_cmds = []

    def fake_run(cmd, capture_output=True, text=True):
        captured_cmds.append(cmd)
        # 模拟 run.py：在 --out 库写一个标准 run 行
        out = cmd[cmd.index("--out") + 1]
        conn = sqlite3.connect(out)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS runs (run_id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " created_at TEXT, strategy TEXT, start_date TEXT, end_date TEXT,"
            " initial_capital REAL, config_json TEXT, status TEXT, stats_json TEXT)"
        )
        conn.execute(
            "INSERT INTO runs (created_at, strategy, start_date, end_date,"
            " initial_capital, config_json, status, stats_json) "
            "VALUES ('2026','demo','20240101','20240131',100000,'{}','completed',?)",
            (json.dumps({"total_return": 0.1, "sharpe": 1.0, "max_drawdown": -0.1}),),
        )
        conn.commit()
        conn.close()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(sweep_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "argv", [
        "sweep.py", str(sweep_config),
        "--start", "20240101", "--end", "20240131", "--out", str(out_db),
    ])
    rc = sweep_mod.main()
    assert rc is None  # main 无返回值，退出码靠 sys.exit
    assert len(captured_cmds) == 2
    for cmd in captured_cmds:
        assert "--no-report" in cmd
        # 共享输出库（此前是每组一个临时 db，compare.py 读不到）
        assert cmd[cmd.index("--out") + 1] == str(out_db)
    conn = sqlite3.connect(str(out_db))
    n_runs = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    n_sweep = conn.execute("SELECT COUNT(*) FROM sweep_results").fetchone()[0]
    conn.close()
    assert n_runs == 2 and n_sweep == 2
