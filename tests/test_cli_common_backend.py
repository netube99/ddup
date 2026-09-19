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

