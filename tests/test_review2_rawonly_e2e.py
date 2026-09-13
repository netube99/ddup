"""V2 audit scratch (F-ML-03): raw-only panel model end-to-end + fail-fast guards.

Locks three behaviors:

1. Raw-only panel model (features = raw backend columns only, no library
   factors) runs the FULL engine: strategy_loader attaches FACTOR_NODES={}
   for the empty closure, engine._build_factor_plan builds an empty factor
   plan instead of raising, runtime.materialize_predictions writes a real
   (non-NaN, varying) ml_<name> column BEFORE the strategy consumes it, and
   run() completes with the model scoring driving selection. Training side
   (ml.dataset.build_panel) accepts the same spec — train/serve symmetric.
2. A hand-built strategy with FACTOR_SPECS but no FACTOR_NODES attribute
   still fails fast (the `nodes is None` guard is not weakened to `{}`
   acceptance for loader-bypassing callers).
3. factor_specs referencing a holding-scope ml_ column is still rejected.
"""

import hashlib
import sqlite3

import numpy as np
import pytest

pytest.importorskip("onnxruntime", reason="需要 onnxruntime")
pytest.importorskip("xgboost", reason="需要 xgboost")
pytest.importorskip("onnxmltools", reason="需要 onnxmltools")

from btcore.engine import Engine
from btcore.ml.dataset import build_panel
from btcore.provider import DataProvider
from btcore.strategy import Strategy
from btcore.strategy_loader import build_strategy
from tests.conftest import MlTopKStrategy, MockDataBackend, write_meta

RAW_FEATURES = ["turnover_rate", "volume_ratio"]


def _make_model(tmp_path, name, factors, raw, state_features=(), classifier=False):
    """训练小 XGBoost 模型并导出 ONNX + meta v3（raw/holding 两种 scope 共用）。"""
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType
    from xgboost import XGBClassifier, XGBRegressor

    n = len(factors) + len(raw) + len(state_features)
    rng = np.random.RandomState(7)
    x = rng.randn(400, n).astype(np.float32)
    if classifier:
        model = XGBClassifier(n_estimators=8, max_depth=3).fit(
            x, (x[:, -1] > 0).astype(np.int32),
        )
    else:
        model = XGBRegressor(n_estimators=8, max_depth=3).fit(
            x, x[:, 0] * 0.8 - x[:, 1] * 0.2,
        )
    onx = convert_xgboost(
        model,
        initial_types=[("float_input", FloatTensorType([None, n]))],
        target_opset=15,
    )
    art = tmp_path / f"{name}.onnx"
    blob = onx.SerializeToString()
    art.write_bytes(blob)
    write_meta(
        tmp_path / f"{name}.meta.json",
        name=name,
        features={"factors": list(factors), "raw": list(raw)},
        state_features=list(state_features),
        post_transform="none",
        scaler_mean=[0.0] * n,
        scaler_std=[1.0] * n,
        artifact_sha256=hashlib.sha256(blob).hexdigest(),
    )
    return art


def _make_raw_only_model(tmp_path, name="raw_only"):
    """训练一个仅用 raw 列特征的小回归模型，导出 ONNX + meta v3。"""
    return _make_model(tmp_path, name, factors=[], raw=list(RAW_FEATURES))


def _make_holding_scope_model(tmp_path, name="hold_m"):
    """含 state_features 的 holding scope 模型（factors + state，共 2 特征）。"""
    return _make_model(
        tmp_path, name, factors=["mom20"], raw=[], state_features=["hold_days"],
        classifier=True,
    )


def test_raw_only_panel_model_full_engine_run(tmp_path):
    """raw-only panel 模型：loader 空 FACTOR_NODES → 引擎空计划 → ml 列物化
    → select 消费 → 完整 run；ml 列非全 NaN（空闭包不产生静默全 NaN 分数）。"""
    art = _make_raw_only_model(tmp_path)
    strategy = build_strategy(
        MlTopKStrategy,
        {"initial_capital": 1_000_000, "max_positions": 3},
        factor_specs=[{"name": "ml_raw_only", "weight": 1.0}],
        models={"raw_only": {"artifact": str(art)}},
        strategy_dir=str(tmp_path),
    )
    # loader：specs 非空但闭包为空 → 挂空 FACTOR_NODES（is not None 语义）
    assert strategy.FACTOR_NODES == {}
    assert strategy.MODEL_SPECS[0].scope == "panel"
    assert strategy.MODEL_SPECS[0].features == []
    assert strategy.MODEL_SPECS[0].raw_features == RAW_FEATURES

    db = str(tmp_path / "r.db")
    engine = Engine(strategy, DataProvider(MockDataBackend()), db_path=db)
    engine.run("20240603", "20240614")

    # 空因子计划仍为 truthy dict：无 topo、无主面板列、无广度需求
    fplan = engine._build_factor_plan()
    assert fplan is not None
    assert fplan["topo"] == []
    assert not fplan["main_columns"]
    assert not fplan["needs"]["market"]

    # ml 列在 select 消费前已物化为真实分数（非全 NaN、有区分度）
    col = engine.bars_df["ml_raw_only"]
    assert col.notna().all()
    assert col.nunique() > 1

    # select 真实读到了 ml 列并据此交易（缺列会被 eval_factor_specs 报错）
    assert len(engine.strategy.FACTOR_SPECS) == 1
    trades = engine.account.holdings, engine.run_id
    conn = sqlite3.connect(db)
    n_trades = conn.execute("SELECT COUNT(*) FROM trade_log").fetchone()[0]
    n_pred = conn.execute(
        "SELECT COUNT(*) FROM ml_predictions WHERE model='raw_only'"
    ).fetchone()[0]
    conn.close()
    assert trades[1] > 0
    assert n_trades > 0, "raw-only 模型评分应驱动买入成交"
    assert n_pred > 0, "panel 模型分数应落库 ml_predictions"


def test_raw_only_train_panel_parity(tmp_path):
    """训练侧 build_panel 对同一 raw-only spec 正常出面板（训练/回测同源）。"""
    art = _make_raw_only_model(tmp_path)
    spec = pytest.importorskip("btcore.ml.spec").ModelSpec.from_dict(
        "raw_only", {"artifact": str(art)}, str(tmp_path),
    )
    backend = MockDataBackend()
    panel = build_panel(backend, None, "20240603", "20240614", spec, {})
    for col in RAW_FEATURES:
        assert col in panel.columns
    assert panel["turnover_rate"].notna().all()


def test_specs_without_nodes_still_fail_fast():
    """手搓策略（绕过 loader）：FACTOR_SPECS 有值但无 FACTOR_NODES → fail-fast。"""

    class S(Strategy):
        def select(self, bars, snapshot, provider):
            return {"buy": [], "sell": []}

    s = S(config={}, factor_specs=[{"name": "mom20", "weight": 1.0}])
    assert s.FACTOR_NODES is None  # 基类默认，未被 loader 挂接
    engine = Engine(s, DataProvider(MockDataBackend()))
    with pytest.raises(ValueError, match="FACTOR_NODES"):
        engine._build_factor_plan()


def test_holding_scope_ml_column_still_rejected(tmp_path):
    """holding scope 模型列进 factor_specs 仍被 loader 拒绝（不因空闭包放宽）。"""
    art = _make_holding_scope_model(tmp_path)
    with pytest.raises(ValueError, match="holding scope"):
        build_strategy(
            MlTopKStrategy,
            {"initial_capital": 1_000_000},
            factor_specs=[{"name": "ml_hold_m", "weight": 1.0}],
            models={"hold_m": {"artifact": str(art)}},
            strategy_dir=str(tmp_path),
        )
