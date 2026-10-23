"""运行清单覆盖回测有效身份，并保留旧 runs.config_json 读取方式。"""

import json
from types import SimpleNamespace

import duckdb
import numpy as np
import pytest

from btcore import run_manifest
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.run_manifest import build_run_manifest, serialize_run_config
from btcore.strategy import Strategy
from tests.conftest import MockDataBackend


class ManifestStrategy(Strategy):
    def select(self, bars, snapshot, provider):
        return {}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def _strategy(weight=1.0, node_expr="close_hfq / pre_close", min_price=5):
    strategy = ManifestStrategy(
        config={"initial_capital": 100_000, "custom": "value"},
        factor_specs=[{"name": "momentum", "weight": weight, "ascending": False}],
        filter_rules={"min_price": min_price},
    )
    strategy.FACTOR_NODES = {"momentum": {"expr": node_expr}}
    return strategy


def test_factor_weight_and_declarations_change_manifest_but_not_legacy_flat_config():
    first = _strategy(weight=1)
    second = _strategy(weight=2)

    # This is the old persistence shape: strategy.config omitted loader declarations.
    assert json.dumps(first.config, sort_keys=True) == json.dumps(second.config, sort_keys=True)

    first_json = serialize_run_config(
        first, 200_000, 7, "000300.SH", backend=object()
    )
    second_json = serialize_run_config(
        second, 200_000, 7, "000300.SH", backend=object()
    )
    first_config, second_config = json.loads(first_json), json.loads(second_json)
    assert first_config["initial_capital"] == 200_000
    assert first_config["max_positions"] == 7
    assert first_config["_run_manifest"]["factor_specs"] != (
        second_config["_run_manifest"]["factor_specs"]
    )
    assert first_config["_run_manifest"]["factor_nodes"] == (
        second_config["_run_manifest"]["factor_nodes"]
    )


def test_engine_persists_factor_weight_in_run_config_json(tmp_path):
    db_path = tmp_path / "runs.duckdb"
    for weight in (1, 2):
        engine = Engine(
            _strategy(weight), DataProvider(MockDataBackend()),
            db_path=str(db_path),
        )
        engine.run("20240603", "20240607")
    with duckdb.connect(str(db_path), read_only=True) as conn:
        rows = conn.execute(
            "SELECT config_json FROM runs ORDER BY run_id"
        ).fetchall()
    manifests = [json.loads(row[0])["_run_manifest"] for row in rows]
    assert [item["factor_specs"][0]["weight"] for item in manifests] == [1, 2]


def test_manifest_records_filter_closure_and_effective_config():
    strategy = _strategy(min_price=8, node_expr="close_hfq / pre_close + 1")
    strategy.config["commission_rate"] = 0.002
    strategy.config["slippage_ticks"] = 5
    manifest = build_run_manifest(
        strategy,
        initial_capital=200_000,
        max_positions=7,
        benchmark="000300.SH",
        backend=object(),
    )
    assert manifest["filter_rules"]["min_price"] == 8
    assert manifest["factor_nodes"]["momentum"]["expr"].endswith("+ 1")
    assert manifest["effective_config"]["initial_capital"] == 200_000
    assert manifest["effective_config"]["benchmark"] == "000300.SH"
    assert manifest["effective_config"]["execution_price"] == "open"
    assert manifest["effective_config"]["commission_rate"] == 0.002
    assert manifest["effective_config"]["slippage_ticks"] == 5
    assert manifest["data_source"]["identity_strength"] == "unknown"


def test_manifest_changes_for_filter_and_factor_node_changes():
    base = build_run_manifest(
        _strategy(), initial_capital=100_000, max_positions=20,
        benchmark=None, backend=object(),
    )
    changed_filter = build_run_manifest(
        _strategy(min_price=6), initial_capital=100_000, max_positions=20,
        benchmark=None, backend=object(),
    )
    changed_node = build_run_manifest(
        _strategy(node_expr="close_hfq / pre_close + 1"),
        initial_capital=100_000, max_positions=20, benchmark=None, backend=object(),
    )
    assert base["manifest_sha256"] != changed_filter["manifest_sha256"]
    assert base["manifest_sha256"] != changed_node["manifest_sha256"]


def test_manifest_records_model_artifact_and_full_meta(tmp_path):
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"model-content")
    strategy = _strategy()
    strategy.MODEL_SPECS = [SimpleNamespace(
        name="alpha", artifact=str(artifact), scope="panel", features=["momentum"],
        raw_features=["turnover_rate"], state_features=[], post_transform="xs_rank",
        scaler_mean=[0.2, 0.3], scaler_std=[1.0, 2.0],
        meta={"features": {"factors": ["momentum"]}, "version": 3},
    )]
    manifest = build_run_manifest(
        strategy, initial_capital=100_000, max_positions=20,
        benchmark=None, backend=object(),
    )
    model = manifest["models"][0]
    assert model["artifact"]["sha256"]
    assert model["meta"]["version"] == 3
    assert model["post_transform"] == "xs_rank"


def test_serialization_is_stable_and_accepts_numpy_scalars(tmp_path):
    strategy = _strategy()
    strategy.config["numpy_value"] = np.int64(4)
    backend = SimpleNamespace(db_path=str(tmp_path / "market.duckdb"), snapshot_id="snap-7")
    first = serialize_run_config(strategy, 100_000, 20, None, backend)
    second = serialize_run_config(strategy, 100_000, 20, None, backend)
    assert first == second
    data_source = json.loads(first)["_run_manifest"]["data_source"]
    assert data_source["snapshot_id"] == "snap-7"
    assert data_source["identity_strength"] == "provider_snapshot"


def test_data_source_file_identity_is_weak_and_empty_snapshot_is_unknown(tmp_path):
    market_db = tmp_path / "market.duckdb"
    market_db.write_bytes(b"market-snapshot")
    backend = SimpleNamespace(db_path=str(market_db), snapshot_id="")
    manifest = build_run_manifest(
        _strategy(), initial_capital=100_000, max_positions=20,
        benchmark=None, backend=backend,
    )
    source = manifest["data_source"]
    assert source["source"]["size_bytes"] == len(b"market-snapshot")
    assert source["source"]["mtime_ns"] is not None
    assert source["identity_strength"] == "unknown"
    assert source["snapshot_id"] == ""


def test_serialization_rejects_unrepresented_values_and_reserved_key():
    strategy = _strategy()
    strategy.config["bad"] = object()
    with pytest.raises(ValueError, match="不支持的值类型"):
        serialize_run_config(strategy, 100_000, 20, None, object())
    strategy.config.pop("bad")
    strategy.config["_run_manifest"] = {}
    with pytest.raises(ValueError, match="保留键冲突"):
        serialize_run_config(strategy, 100_000, 20, None, object())


def test_missing_git_executable_does_not_block_run_manifest(monkeypatch):
    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git unavailable")

    monkeypatch.setattr(run_manifest.subprocess, "run", missing_git)
    manifest = build_run_manifest(
        _strategy(), initial_capital=100_000, max_positions=20,
        benchmark=None, backend=object(),
    )
    assert manifest["code"]["git_commit"] is None


def test_engine_manifest_records_constructor_values_after_config_mutation():
    strategy = _strategy()
    engine = Engine(
        strategy, DataProvider(MockDataBackend()), initial_capital=200_000,
        max_positions=7,
    )
    strategy.config["slippage_ticks"] = 9
    serialized = serialize_run_config(
        strategy,
        engine.initial_capital,
        engine.max_positions,
        engine.benchmark,
        engine.provider.backend,
        effective_config=engine._run_effective_config,
    )
    manifest = json.loads(serialized)["_run_manifest"]
    assert manifest["effective_config"]["slippage_ticks"] == 2
    assert manifest["effective_config"]["commission_rate"] == 0.00015
    assert manifest["effective_config"]["initial_capital"] == 200_000
    assert manifest["effective_config"]["max_positions"] == 7


def test_old_flat_config_remains_readable_by_report_reader(tmp_path):
    from btcore import database
    from research.report import load_runs

    db_path = str(tmp_path / "legacy.duckdb")
    conn = database.init_backtest_db(db_path)
    old_config = {"initial_capital": 100_000, "max_positions": 5, "custom": "x"}
    database.write_run(
        conn,
        created_at="2026-01-01T00:00:00",
        strategy="Legacy",
        start_date="20260101",
        end_date="20260102",
        initial_capital=100_000,
        config_json=json.dumps(old_config),
        status="completed",
    )
    conn.close()

    loaded = load_runs(db_path, [1])[0]["meta"]
    assert json.loads(loaded["config_json"]) == old_config

