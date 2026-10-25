"""DDUP_BACKEND 后端选择测试（research/cli_common.py）与 CLI 输入错误通道
（scripts/run.py、scripts/dump_fixtures.py）回归。"""

import sys
import types

import duckdb
import pytest

from research import cli_common


class _FakeBackend:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_default_backend_spec():
    assert cli_common.DEFAULT_BACKEND == "adapters.tushare:TushareBackend"


def test_load_backend_env_override(monkeypatch):
    mod = types.ModuleType("fake_gold_backend_for_test")
    mod.FakeBackend = _FakeBackend
    monkeypatch.setitem(sys.modules, "fake_gold_backend_for_test", mod)
    monkeypatch.setenv("DDUP_BACKEND", "fake_gold_backend_for_test:FakeBackend")
    backend = cli_common._load_backend()
    assert isinstance(backend, _FakeBackend)


def test_load_backend_bad_spec(monkeypatch):
    monkeypatch.setenv("DDUP_BACKEND", "no_colon_here")
    with pytest.raises(ValueError, match="module:Class"):
        cli_common._load_backend()


def test_load_backend_missing_class(monkeypatch):
    mod = types.ModuleType("fake_gold_backend_for_test2")
    monkeypatch.setitem(sys.modules, "fake_gold_backend_for_test2", mod)
    monkeypatch.setenv("DDUP_BACKEND", "fake_gold_backend_for_test2:NoSuchClass")
    with pytest.raises(ValueError, match="不存在"):
        cli_common._load_backend()


def test_fail_prints_stderr_and_returns_1(capsys):
    assert cli_common.fail("坏输入") == 1
    assert "错误：坏输入" in capsys.readouterr().err


def test_validate_date_range_ok():
    assert cli_common.validate_date_range("20240101", "20240630") is None


@pytest.mark.parametrize("start,end,match", [
    ("2024-01-01", "20240201", "8 位数字"),
    ("2024011", "20240201", "8 位数字"),
    ("abcdefgh", "20240201", "8 位数字"),
    ("20240301", "20240201", "颠倒"),
])
def test_validate_date_range_rejects(start, end, match):
    msg = cli_common.validate_date_range(start, end)
    assert msg is not None and match in msg


# ═══════════════════════════════════════════
# scripts/run.py 用户输入错误：可读消息 + exit 1
# ═══════════════════════════════════════════


def _run_main(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["run.py", *argv])
    from scripts import run

    return run.main()


def test_run_bad_date_format(tmp_path, monkeypatch, capsys):
    rc = _run_main(monkeypatch, [
        str(tmp_path / "s.yaml"), "--start", "2024-01-01", "--end", "20240201",
    ])
    assert rc == 1
    assert "8 位数字" in capsys.readouterr().err


def test_run_start_after_end(tmp_path, monkeypatch, capsys):
    rc = _run_main(monkeypatch, [
        str(tmp_path / "s.yaml"), "--start", "20240630", "--end", "20240101",
    ])
    assert rc == 1
    assert "颠倒" in capsys.readouterr().err


def test_run_missing_yaml(tmp_path, monkeypatch, capsys):
    rc = _run_main(monkeypatch, [
        str(tmp_path / "nope.yaml"), "--start", "20240101", "--end", "20240131",
    ])
    assert rc == 1
    assert "不存在" in capsys.readouterr().err


def test_run_yaml_syntax_error(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "bad.yaml"
    bad.write_text("a: [\n", encoding="utf-8")
    rc = _run_main(monkeypatch, [
        str(bad), "--start", "20240101", "--end", "20240131",
    ])
    assert rc == 1
    assert "解析失败" in capsys.readouterr().err


def test_run_yaml_not_mapping(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "list.yaml"
    bad.write_text("- a\n- b\n", encoding="utf-8")
    rc = _run_main(monkeypatch, [
        str(bad), "--start", "20240101", "--end", "20240131",
    ])
    assert rc == 1
    assert "策略配置错误" in capsys.readouterr().err


# ═══════════════════════════════════════════
# INFRA-04: bars 联表 SQL 列限定（DuckDB Ambiguous reference 回归）
# ═══════════════════════════════════════════


def _dump_fixtures_module(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["dump_fixtures.py"])
    import scripts.dump_fixtures as mod

    return mod


def test_dump_fixtures_bars_clause_all_qualified(monkeypatch):
    mod = _dump_fixtures_module(monkeypatch)
    clause = mod._bars_select_clause()
    assert clause.split(", ") == [f"s.{c}" for c in mod.BAR_COLUMNS]


def test_dump_fixtures_bars_clause_runs_on_twin_columns(monkeypatch):
    mod = _dump_fixtures_module(monkeypatch)
    cols = ", ".join(
        f"{c} {'VARCHAR' if c in ('ts_code', 'trade_date') else 'DOUBLE'}"
        for c in mod.BAR_COLUMNS
    )
    conn = duckdb.connect(":memory:")
    try:
        conn.execute(f"CREATE TABLE stk_factor_pro ({cols})")
        conn.execute("CREATE TABLE bak_basic (ts_code VARCHAR, trade_date VARCHAR, eps DOUBLE)")
        conn.execute(
            f"SELECT {mod._bars_select_clause()}, b.eps AS eps FROM stk_factor_pro s "
            "LEFT JOIN bak_basic b ON b.ts_code = s.ts_code AND b.trade_date = s.trade_date"
        )
    finally:
        conn.close()



def test_load_backend_missing_module(monkeypatch):
    """F-CLI-07：模块不存在与格式错误/类不存在同走 ValueError 通道。"""
    monkeypatch.setenv("DDUP_BACKEND", "nope_module_for_test:Nope")
    with pytest.raises(ValueError, match="模块不存在"):
        cli_common._load_backend()


def test_load_backend_missing_parent_package(monkeypatch):
    """父包不存在同样视为配置错误（ModuleNotFoundError.name 指向父包）。"""
    monkeypatch.setenv("DDUP_BACKEND", "nope_pkg_for_test.sub:Nope")
    with pytest.raises(ValueError, match="模块不存在"):
        cli_common._load_backend()


def test_load_backend_internal_dependency_error_kept(tmp_path, monkeypatch):
    """模块内部依赖缺失保留原始 ModuleNotFoundError，不误报为模块不存在。"""
    mod_file = tmp_path / "fake_broken_backend_for_test.py"
    mod_file.write_text("import definitely_missing_dep_xyz\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setenv("DDUP_BACKEND", "fake_broken_backend_for_test:Backend")
    with pytest.raises(ModuleNotFoundError, match="definitely_missing_dep_xyz"):
        cli_common._load_backend()


def test_run_report_write_failure_clean_channel(tmp_path, monkeypatch, capsys):
    """F-CLI-06：--report 写失败 → 可读错误 + exit 1，不裸 traceback。

    回测结果已打印、provider 已关闭（与 scripts/report.py 同一写操作的
    错误通道对齐）。触发等价于 report.py:518 的 FileNotFoundError。
    """
    from scripts import run

    yaml_path = tmp_path / "s.yaml"
    yaml_path.write_text("initial_capital: 100000\n", encoding="utf-8")

    class _Backend:
        closed = False

        def close(self):
            self.closed = True

    backend = _Backend()

    class _Strategy:
        pass

    class _Engine:
        def __init__(self, strategy, provider, initial_capital=None,
                     db_path=None):
            pass

        def run(self, start, end):
            return {"statistics": {"total_return": 0.1}, "trade_log": []}

    def _boom(*args, **kwargs):
        raise FileNotFoundError(
            2, "No such file or directory", "/nonexistent_dir/r.html")

    monkeypatch.setattr(run, "load_strategy", lambda p: _Strategy())
    monkeypatch.setattr(run, "Engine", _Engine)
    monkeypatch.setattr(run, "generate_report", _boom)
    monkeypatch.setattr(run.cli_common, "make_provider",
                        lambda: types.SimpleNamespace(backend=backend))
    monkeypatch.setattr(sys, "argv", [
        "run.py", str(yaml_path), "--start", "20240603", "--end", "20240628",
        "--report", str(tmp_path / "no_dir" / "r.html"),
    ])
    rc = run.main()
    captured = capsys.readouterr()
    assert rc == 1
    assert "无法写报告" in captured.err
    assert "Traceback" not in captured.err
    # 回测已完成：统计已打印，报告路径不再被宣称写出
    assert "total_return" in captured.out
    assert "report:" not in captured.out
    assert backend.closed
