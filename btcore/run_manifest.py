"""Build a bounded, versioned identity record for a backtest run."""

import dataclasses
import hashlib
import importlib.metadata
import json
import logging
import math
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

from btcore.constants import (
    COMMISSION_RATE,
    MIN_COMMISSION,
    STAMP_TAX_RATE,
    TRANSFER_FEE_RATE,
)

logger = logging.getLogger(__name__)
MANIFEST_KEY = "_run_manifest"
MANIFEST_VERSION = 1


def build_run_manifest(
    strategy,
    *,
    initial_capital: float,
    max_positions: int,
    benchmark: str | None,
    backend,
    effective_config: dict | None = None,
) -> dict:
    """Capture effective declarations and content identities before strategy hooks run."""
    config = _normalize(strategy.config)
    config.update({"initial_capital": initial_capital, "max_positions": max_positions})
    config["benchmark"] = benchmark
    defaults = {
        "execution_price": "open",
        "slippage_ticks": 2,
        "condition_slippage_ticks": None,
        "order_volume_ratio": None,
        "quiet_skips": False,
        "ml_log": None,
        "commission_rate": COMMISSION_RATE,
        "min_commission": MIN_COMMISSION,
        "stamp_tax_rate": STAMP_TAX_RATE,
        "transfer_fee_rate": TRANSFER_FEE_RATE,
    }
    for key, value in defaults.items():
        config.setdefault(key, value)
    config.update(_normalize(effective_config or {}))

    models = []
    for spec in getattr(strategy, "MODEL_SPECS", None) or []:
        artifact = Path(spec.artifact)
        models.append({
            "name": spec.name,
            "scope": spec.scope,
            "artifact": _file_identity(artifact),
            "features": _normalize(spec.features),
            "raw_features": _normalize(spec.raw_features),
            "state_features": _normalize(spec.state_features),
            "post_transform": spec.post_transform,
            "scaler_mean": _normalize(spec.scaler_mean),
            "scaler_std": _normalize(spec.scaler_std),
            "meta": _normalize(spec.meta),
            "meta_sha256": _sha256_json(spec.meta),
        })

    source_path = _source_path(strategy.__class__)
    nodes = _normalize(getattr(strategy, "FACTOR_NODES", None))
    manifest = {
        "schema_version": MANIFEST_VERSION,
        "effective_config": config,
        "factor_specs": _normalize(getattr(strategy, "FACTOR_SPECS", [])),
        "filter_rules": _normalize(getattr(strategy, "FILTER_RULES", {})),
        "factor_nodes": nodes,
        "factor_nodes_sha256": _sha256_json(nodes),
        "strategy": {
            "class": f"{strategy.__class__.__module__}:{strategy.__class__.__qualname__}",
            "source": _file_identity(source_path) if source_path else None,
        },
        "code": {
            "git_commit": _git_commit(),
            "engine_sha256": _sha256_file(Path(__file__).with_name("engine.py")),
            "dependencies": _dependency_versions(),
        },
        "models": models,
        "data_source": _data_source_identity(backend),
    }
    manifest["manifest_sha256"] = _sha256_json(manifest)
    return manifest


def serialize_run_config(
    strategy,
    initial_capital: float,
    max_positions: int,
    benchmark: str | None,
    backend,
    effective_config: dict | None = None,
) -> str:
    """Preserve legacy flat config fields and add the versioned run manifest."""
    legacy_config = _normalize(strategy.config)
    if MANIFEST_KEY in legacy_config:
        raise ValueError(f"策略 config 保留键冲突: {MANIFEST_KEY}")
    legacy_config["initial_capital"] = initial_capital
    legacy_config["max_positions"] = max_positions
    legacy_config[MANIFEST_KEY] = build_run_manifest(
        strategy,
        initial_capital=initial_capital,
        max_positions=max_positions,
        benchmark=benchmark,
        backend=backend,
        effective_config=effective_config,
    )
    try:
        return json.dumps(legacy_config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"运行配置不能序列化为 JSON: {exc}") from exc


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"运行配置包含非有限数值: {value!r}")
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _normalize(dataclasses.asdict(value))
    if hasattr(value, "item") and callable(value.item):
        try:
            return _normalize(value.item())
        except (TypeError, ValueError):
            pass
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("运行配置 mapping 的键必须是字符串")
        return {key: _normalize(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise ValueError(f"运行配置包含不支持的值类型: {type(value).__name__}")


def _source_path(cls: type) -> Path | None:
    try:
        import inspect

        source = inspect.getsourcefile(cls)
    except (TypeError, OSError):
        return None
    return Path(source).resolve() if source else None


def _file_identity(path: Path) -> dict:
    return {"path": str(path), "sha256": _sha256_file(path) if path.is_file() else None}


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_json(value: Any) -> str:
    payload = json.dumps(
        _normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _git_commit() -> str | None:
    repo = Path(__file__).resolve().parents[1]
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
            text=True, check=False, timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("无法读取 Git commit，运行清单将记录 null: %s", exc)
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def _dependency_versions() -> dict[str, str | None]:
    versions = {}
    for package in (
        "ddup", "numpy", "pandas", "duckdb", "PyYAML", "numexpr", "onnxruntime"
    ):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    versions["python"] = sys.version.split()[0]
    return versions


def _data_source_identity(backend) -> dict:
    attributes = ("db_path", "database_path", "data_path", "path")
    source = next((getattr(backend, name) for name in attributes
                   if isinstance(getattr(backend, name, None), (str, Path))), None)
    snapshot_id = getattr(backend, "snapshot_id", None)
    source_info = None
    if source is not None:
        path = Path(source)
        try:
            stat = path.stat() if path.is_file() else None
        except OSError:
            stat = None
        source_info = {
            "path": str(path),
            "size_bytes": stat.st_size if stat else None,
            "mtime_ns": stat.st_mtime_ns if stat else None,
        }
    if snapshot_id is not None:
        strength = "provider_snapshot" if snapshot_id else "unknown"
    elif source_info is not None:
        strength = "weak"
    else:
        strength = "unknown"
    return {
        "backend": f"{type(backend).__module__}:{type(backend).__qualname__}",
        "source": source_info,
        "snapshot_id": _normalize(snapshot_id) if snapshot_id is not None else None,
        "identity_strength": strength,
    }
