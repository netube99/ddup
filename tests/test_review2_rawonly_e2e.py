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
import json
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
from btcore.strategy_tools import bars_to_df, eval_factor_specs
from tests.conftest import MockDataBackend

RAW_FEATURES = ["turnover_rate", "volume_ratio"]


class _MlTopK(Strategy):
    """每日读 ml_<name> 物化列评分，买 top1，卖出不在名单的持仓。"""

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        df = bars_to_df(bars)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)
        top = score.nlargest(1).index.tolist()
        buys = [s for s in top if s not in snapshot.holdings]
        sells = [s for s in snapshot.holdings if s not in top]
        return {"buy": buys, "sell": sells}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def _make_raw_only_model(tmp_path, name="raw_only"):
    """训练一个仅用 raw 列特征的小回归模型，导出 ONNX + meta v3。"""
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType
    from xgboost import XGBRegressor

    rng = np.random.RandomState(7)
    x = rng.randn(400, len(RAW_FEATURES)).astype(np.float32)
    model = XGBRegressor(n_estimators=8, max_depth=3).fit(
        x, x[:, 0] * 0.8 - x[:, 1] * 0.2,
    )
    onx = convert_xgboost(
        model,
        initial_types=[("float_input", FloatTensorType([None, len(RAW_FEATURES)]))],
        target_opset=15,
    )
    art = tmp_path / f"{name}.onnx"
    blob = onx.SerializeToString()
    art.write_bytes(blob)
    meta = {
        "version": 3,
        "name": name,
        "features": {"factors": [], "raw": list(RAW_FEATURES)},
        "state_features": [],
        "post_transform": "none",
        "label": {"type": "xs_fwdret", "horizon": 5},
        "train_window": ["20240101", "20240630"],
        "scaler_mean": [0.0] * len(RAW_FEATURES),
        "scaler_std": [1.0] * len(RAW_FEATURES),
        "artifact_sha256": hashlib.sha256(blob).hexdigest(),
    }
    (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta))
    return art


def _make_holding_scope_model(tmp_path, name="hold_m"):
    """含 state_features 的 holding scope 模型（factors + state，共 2 特征）。"""
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType
    from xgboost import XGBClassifier

    rng = np.random.RandomState(7)
    x = rng.randn(400, 2).astype(np.float32)
    model = XGBClassifier(n_estimators=8, max_depth=3).fit(
        x, (x[:, -1] > 0).astype(np.int32),
    )
    onx = convert_xgboost(
        model,
        initial_types=[("float_input", FloatTensorType([None, 2]))],
        target_opset=15,
    )
    art = tmp_path / f"{name}.onnx"
    blob = onx.SerializeToString()
    art.write_bytes(blob)
    meta = {
        "version": 3,
        "name": name,
        "features": {"factors": ["mom20"], "raw": []},
        "state_features": ["hold_days"],
        "post_transform": "none",
        "label": {"type": "xs_fwdret", "horizon": 5},
        "train_window": ["20240101", "20240630"],
        "scaler_mean": [0.0] * 2,
        "scaler_std": [1.0] * 2,
        "artifact_sha256": hashlib.sha256(blob).hexdigest(),
    }
    (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta))
    return art


def test_raw_only_panel_model_full_engine_run(tmp_path):
    """raw-only panel 模型：loader 空 FACTOR_NODES → 引擎空计划 → ml 列物化
    → select 消费 → 完整 run；ml 列非全 NaN（空闭包不产生静默全 NaN 分数）。"""
    art = _make_raw_only_model(tmp_path)
    strategy = build_strategy(
        _MlTopK,
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
            _MlTopK,
            {"initial_capital": 1_000_000},
            factor_specs=[{"name": "ml_hold_m", "weight": 1.0}],
            models={"hold_m": {"artifact": str(art)}},
            strategy_dir=str(tmp_path),
        )
