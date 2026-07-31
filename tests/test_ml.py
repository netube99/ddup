"""ML 子系统测试：spec 解析 / loader 整合 / runtime 推理 / 引擎双 scope / 训练同源。

ONNX 相关用例需要 onnxruntime + xgboost + onnxmltools（开发依赖，
未声明在 pyproject）——缺失时整组跳过。
"""

import hashlib
import json
import sqlite3

import numpy as np
import pandas as pd
import pytest

from btcore.ml.spec import ModelSpec
from btcore.strategy_loader import build_strategy
from tests.conftest import MockDataBackend

# ── 纯解析/整合用例（无 ONNX 依赖）──


def _write_meta(path, **over):
    meta = {
        "version": 2,
        "name": path.stem,
        "features": {"factors": ["mom20"], "raw": ["turnover_rate"]},
        "state_features": [],
        "post_transform": "xs_rank",
        "label": {"type": "xs_fwdret", "horizon": 5},
        "train_window": ["20240101", "20240630"],
        "scaler_mean": [0.0, 0.0],
        "scaler_std": [1.0, 1.0],
    }
    meta.update(over)
    path.write_text(json.dumps(meta))


class TestModelSpec:
    def test_ok_panel_scope(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json")
        spec = ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))
        assert spec.scope == "panel"
        assert spec.column == "ml_a"
        assert spec.feature_order == ["mom20", "turnover_rate"]
        assert spec.post_transform == "xs_rank"

    def test_holding_scope_derived_from_state_features(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json", state_features=["hold_days"],
                    post_transform="none")
        spec = ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))
        assert spec.scope == "holding"

    def test_missing_artifact(self, tmp_path):
        with pytest.raises(ValueError, match="artifact 不存在"):
            ModelSpec.from_dict("a", {"artifact": "nope.onnx"}, str(tmp_path))

    def test_missing_meta_engine_path(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        with pytest.raises(ValueError, match="缺少 meta"):
            ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))

    def test_bootstrap_from_yaml_inline_features(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        spec = ModelSpec.from_dict(
            "a",
            {"artifact": "a.onnx",
             "features": {"factors": ["mom20"], "raw": [], "state": ["hold_days"]}},
            str(tmp_path),
            require_meta=False,
        )
        assert spec.scope == "holding"
        assert spec.state_features == ["hold_days"]

    def test_bad_meta_version(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json", version=1)
        with pytest.raises(ValueError, match="meta 版本"):
            ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))

    def test_yaml_meta_feature_mismatch(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json")
        with pytest.raises(ValueError, match="不一致"):
            ModelSpec.from_dict(
                "a",
                {"artifact": "a.onnx", "features": {"factors": ["vol_z"], "raw": []}},
                str(tmp_path),
            )

    def test_bad_state_feature(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json", state_features=["magic"])
        with pytest.raises(ValueError, match="不支持的 state_features"):
            ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))

    def test_bad_post_transform(self, tmp_path):
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json", post_transform="magic")
        with pytest.raises(ValueError, match="post_transform"):
            ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))

    def test_role_key_deprecated_but_tolerated(self, tmp_path):
        """旧 YAML 的 role 键仅告警忽略，scope 仍由特征推导。"""
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json")
        spec = ModelSpec.from_dict(
            "a", {"artifact": "a.onnx", "role": "exit_guard"}, str(tmp_path),
        )
        assert spec.scope == "panel"


class TestLoaderIntegration:
    def _models(self, tmp_path, state=(), post="xs_rank"):
        art = tmp_path / "m.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "m.meta.json",
                    state_features=list(state), post_transform=post)
        return {"m": {"artifact": str(art)}}

    def _cls(self):
        from btcore.strategy import Strategy

        class S(Strategy):
            def on_start(self, provider, first_date, end_date=None): pass
            def select(self, bars, snapshot, provider): return {"buy": [], "sell": []}
            def calc_conditions(self, symbol, entry_price, bar, holding_days): return []

        return S

    def test_model_features_join_closure(self, tmp_path):
        strat = build_strategy(
            self._cls(), {},
            factor_specs=[{"factor": "ml_m", "weight": 0.5}],
            models=self._models(tmp_path),
        )
        # ml_ 列成为评分 spec；模型特征因子自动以 materialize_only 并入
        names = {s["name"]: s for s in strat.FACTOR_SPECS}
        assert "ml_m" in names and not names["ml_m"]["materialize_only"]
        assert names["mom20"]["materialize_only"]
        # 模型特征进入因子闭包
        assert "mom20" in strat.FACTOR_NODES
        # raw 特征并入 REQUIRED_FIELDS
        assert "turnover_rate" in strat.REQUIRED_FIELDS
        # run 摘要落 config
        assert strat.config["models_meta"][0]["name"] == "m"
        assert strat.config["models_meta"][0]["scope"] == "panel"

    def test_undeclared_ml_column_rejected(self, tmp_path):
        with pytest.raises(ValueError, match="未声明的模型列"):
            build_strategy(self._cls(), {}, factor_specs=[{"factor": "ml_ghost"}])

    def test_holding_column_rejected_in_factor_specs(self, tmp_path):
        with pytest.raises(ValueError, match="holding scope"):
            build_strategy(
                self._cls(), {},
                factor_specs=[{"factor": "ml_m"}],
                models=self._models(tmp_path, state=("hold_days",), post="none"),
            )

    def test_model_exit_validates_declared_model(self, tmp_path):
        with pytest.raises(ValueError, match="未声明的模型"):
            build_strategy(
                self._cls(),
                {"conditions": {"model_exit": [{"model": "ghost", "threshold": 0.5}]}},
                models=self._models(tmp_path),
            )

    def test_model_exit_validation_shape(self, tmp_path):
        from btcore.strategy_loader import _validate_conditions

        with pytest.raises(ValueError, match="model_exit 必须是 list"):
            _validate_conditions({"model_exit": {"model": "m"}})
        with pytest.raises(ValueError, match="threshold"):
            _validate_conditions({"model_exit": [{"model": "m", "threshold": 2}]})
        ok = _validate_conditions({"model_exit": [{"model": "m", "threshold": 0.6}]})
        assert ok["model_exit"][0]["model"] == "m"


# ── ONNX 相关用例 ──

onnxruntime = pytest.importorskip("onnxruntime", reason="需要 onnxruntime")
xgboost = pytest.importorskip("xgboost", reason="需要 xgboost")
onnxmltools = pytest.importorskip("onnxmltools", reason="需要 onnxmltools")

from onnxmltools import convert_xgboost  # noqa: E402
from onnxmltools.convert.common.data_types import FloatTensorType  # noqa: E402

from btcore.engine import Engine  # noqa: E402
from btcore.ml import runtime as ml_runtime  # noqa: E402
from btcore.provider import DataProvider  # noqa: E402
from btcore.strategy import Strategy  # noqa: E402
from btcore.strategy_tools import (  # noqa: E402
    ConditionBuilder,
    bars_to_df,
    eval_factor_specs,
)


def _make_model(tmp_path, name="m", n_features=2, state=(), post="none"):
    """训练一个可复现的小模型并导出 ONNX + meta v2。"""
    from xgboost import XGBClassifier, XGBRegressor

    rng = np.random.RandomState(7)
    x = rng.randn(400, n_features).astype(np.float32)
    if state:
        model = XGBClassifier(n_estimators=8, max_depth=3).fit(
            x, (x[:, -1] > 0).astype(np.int32),
        )
    else:
        model = XGBRegressor(n_estimators=8, max_depth=3).fit(
            x, x[:, 0] * 0.8 - x[:, 1] * 0.2,
        )
    onx = convert_xgboost(
        model,
        initial_types=[("float_input", FloatTensorType([None, n_features]))],
        target_opset=15,
    )
    art = tmp_path / f"{name}.onnx"
    blob = onx.SerializeToString()
    art.write_bytes(blob)
    factors = ["mom20"] if n_features >= 1 else []
    raw = ["turnover_rate"] if n_features >= 2 else []
    _write_meta(
        tmp_path / f"{name}.meta.json",
        name=name,
        features={"factors": factors, "raw": raw},
        state_features=list(state), post_transform=post,
        scaler_mean=[0.0] * n_features, scaler_std=[1.0] * n_features,
        artifact_sha256=hashlib.sha256(blob).hexdigest(),
    )
    return art


class _TopK(Strategy):
    """每日买评分 top 1，卖出不在名单的持仓。"""

    def on_start(self, provider, first_date, end_date=None): pass

    def select(self, bars, snapshot, provider):
        df = bars_to_df(bars)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)
        top = score.nlargest(1).index.tolist()
        buys = [s for s in top if s not in snapshot.holdings]
        sells = [s for s in snapshot.holdings if s not in top]
        return {"buy": buys, "sell": sells}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


class TestRuntime:
    def test_materialize_xs_rank(self, tmp_path):
        art = _make_model(tmp_path, post="xs_rank")
        spec = ModelSpec.from_dict("m", {"artifact": str(art)}, str(tmp_path))

        idx = pd.MultiIndex.from_tuples(
            [("20240603", "A"), ("20240603", "B"), ("20240604", "A"), ("20240604", "B")],
            names=["trade_date", "symbol"],
        )
        df = pd.DataFrame(
            {"mom20": [1.0, 2.0, 3.0, 4.0], "turnover_rate": [1.0, 1.0, 1.0, 1.0]},
            index=idx,
        )
        ml_runtime.materialize_predictions(df, [spec])
        assert "ml_m" in df.columns
        # xs_rank：每日截面独立排名，值域 (0,1]，mom20 大的排名高
        assert df.loc[("20240603", "B"), "ml_m"] > df.loc[("20240603", "A"), "ml_m"]
        assert df["ml_m"].max() <= 1.0 and df["ml_m"].min() > 0.0

    def test_missing_feature_column_fails(self, tmp_path):
        art = _make_model(tmp_path)
        spec = ModelSpec.from_dict("m", {"artifact": str(art)}, str(tmp_path))
        df = pd.DataFrame(
            {"mom20": [1.0]},
            index=pd.MultiIndex.from_tuples(
                [("20240603", "A")], names=["trade_date", "symbol"],
            ),
        )
        with pytest.raises(ValueError, match="特征列未物化"):
            ml_runtime.materialize_predictions(df, [spec])

    def test_holding_score_state_features(self, tmp_path):
        art = _make_model(
            tmp_path, name="g", n_features=4,
            state=("hold_days", "ret_from_entry"),
        )
        spec = ModelSpec.from_dict("g", {"artifact": str(art)}, str(tmp_path))
        from tests.conftest import make_bar, make_holding

        holding = make_holding(entry_price=10.0, holding_days=5)
        bar = make_bar(close=11.0, mom20=1.0, turnover_rate=1.0, close_hfq=11.0)
        score = ml_runtime.holding_score(spec, bar, holding)
        assert score is not None and 0.0 <= score <= 1.0

        # 特征缺失 > 50% → None。构造 3 市场特征 + 1 账户态的模型：3/4 缺失越界
        art2 = tmp_path / "g2.onnx"
        art2.write_bytes(art.read_bytes())
        _write_meta(
            tmp_path / "g2.meta.json", name="g2",
            features={"factors": ["mom20"], "raw": ["turnover_rate", "pe_ttm"]},
            state_features=["hold_days"], post_transform="none",
            scaler_mean=[0.0] * 4, scaler_std=[1.0] * 4,
        )
        spec2 = ModelSpec.from_dict("g2", {"artifact": str(art2)}, str(tmp_path))
        bar_missing = {"trade_date": "20240603"}
        assert ml_runtime.holding_score(spec2, bar_missing, holding) is None
        # 2/4 缺失 = 恰好 50%，不越界 → 正常打分
        assert ml_runtime.holding_score(spec, {"trade_date": "20240603"}, holding) is not None

    def test_state_feature_formula(self):
        from tests.conftest import make_bar, make_holding

        holding = make_holding(entry_price=10.0, holding_days=7)
        bar = make_bar(close=12.0, close_hfq=12.0)
        state = ml_runtime.compute_state_features(
            ["hold_days", "ret_from_entry"], bar, holding,
        )
        assert state["hold_days"] == 7.0
        assert state["ret_from_entry"] == pytest.approx(0.2)

    def test_flat_post_transform(self):
        s = pd.Series({"A": 1.0, "B": 3.0, "C": 2.0})
        ranked = ml_runtime.apply_post_transform_flat(s, "xs_rank")
        assert ranked["B"] == 1.0 and ranked["A"] == pytest.approx(1 / 3)
        z = ml_runtime.apply_post_transform_flat(s, "xs_zscore")
        assert z.mean() == pytest.approx(0.0, abs=1e-9)


class TestEngineIntegration:
    def test_panel_model_scoring_and_logging(self, tmp_path):
        art = _make_model(tmp_path, post="xs_rank")
        strat = build_strategy(
            _TopK,
            {"initial_capital": 1_000_000, "max_positions": 3},
            factor_specs=[{"factor": "ml_m", "weight": 1.0}],
            models={"m": {"artifact": str(art)}},
        )
        db = str(tmp_path / "r.db")
        engine = Engine(strat, DataProvider(MockDataBackend()), db_path=db)
        engine.run("20240603", "20240614")

        # ml_m 物化进面板并被 select 消费（缺列会 eval_factor_specs 报错）
        assert "ml_m" in engine.bars_df.columns
        assert engine.bars_df["ml_m"].between(0, 1, inclusive="both").all()

        conn = sqlite3.connect(db)
        n = conn.execute("SELECT COUNT(*) FROM ml_predictions").fetchone()[0]
        models = {r[0] for r in conn.execute("SELECT DISTINCT model FROM ml_predictions")}
        conn.close()
        assert n > 0 and models == {"m"}

    def _holding_strategy(self, tmp_path, threshold=0.01):
        """买一只持有不动 + ConditionBuilder model_exit 规则。"""
        art = _make_model(
            tmp_path, name="g", n_features=4,
            state=("hold_days", "ret_from_entry"),
        )

        class HoldAll(Strategy):
            def on_start(self, provider, first_date, end_date=None):
                self._cb = ConditionBuilder(self.config.get("conditions"))

            def select(self, bars, snapshot, provider):
                if not snapshot.holdings:
                    df = bars_to_df(bars)
                    return {"buy": [df.index[0]], "sell": []}
                return {"buy": [], "sell": []}

            def calc_conditions(self, symbol, entry_price, bar, holding_days):
                return self._cb.calc(symbol, entry_price, bar, holding_days)

        return build_strategy(
            HoldAll,
            {"initial_capital": 1_000_000, "max_positions": 3,
             "conditions": {"model_exit": [{"model": "g", "threshold": threshold}]}},
            models={"g": {"artifact": str(art)}},
        )

    def test_holding_score_injected_and_model_exit(self, tmp_path):
        strat = self._holding_strategy(tmp_path)
        db = str(tmp_path / "g.db")
        engine = Engine(strat, DataProvider(MockDataBackend()), db_path=db)
        engine.run("20240603", "20240614")

        conn = sqlite3.connect(db)
        exits = conn.execute(
            "SELECT date, symbol, price FROM trade_log WHERE trigger='ML_EXIT'"
        ).fetchall()
        guard_rows = conn.execute(
            "SELECT COUNT(*) FROM ml_predictions WHERE model='g'"
        ).fetchone()[0]
        conn.close()
        # 阈值 0.01：持仓分数几乎必然越界 → 产生 ML_EXIT（策略经
        # ConditionBuilder 自行解释分数，引擎只注入不判定）
        assert exits, "model_exit 规则应产生 ML_EXIT 成交"
        assert guard_rows > 0
        assert all(p > 0 for _, _, p in exits)

    def test_model_exit_respects_t1_lock(self, tmp_path):
        """ML_EXIT 走 exit_conditions 通道，买入当日锁定的持仓不会被卖出。"""
        strat = self._holding_strategy(tmp_path)
        db = str(tmp_path / "t1.db")
        engine = Engine(strat, DataProvider(MockDataBackend()), db_path=db)
        engine.run("20240603", "20240607")

        conn = sqlite3.connect(db)
        buys = conn.execute(
            "SELECT date, symbol FROM trade_log WHERE side='BUY' ORDER BY date"
        ).fetchall()
        sells = conn.execute(
            "SELECT date, symbol FROM trade_log WHERE trigger='ML_EXIT' ORDER BY date"
        ).fetchall()
        conn.close()
        buy_dates = {}
        for d, s in buys:
            buy_dates.setdefault(s, []).append(d)
        for d, s in sells:
            assert all(d > bd for bd in buy_dates.get(s, []) if bd <= d), \
                f"{s} 在 {d} 的 ML_EXIT 击穿 T+1 锁定"


class TestTrainingPipeline:
    def test_dataset_matches_engine_panel(self, tmp_path):
        """训练面板与引擎 preload 面板逐列一致（同源验收）。"""
        from btcore.factors.library import load_library
        from btcore.ml import dataset

        art = _make_model(tmp_path, post="none")
        strat = build_strategy(
            _TopK,
            {"initial_capital": 1_000_000},
            factor_specs=[{"factor": "ml_m"}],
            models={"m": {"artifact": str(art)}},
        )
        engine = Engine(strat, DataProvider(MockDataBackend()),
                        db_path=str(tmp_path / "c.db"))
        engine.run("20240603", "20240614")

        spec = strat.MODEL_SPECS[0]
        ds = dataset.build_panel(
            MockDataBackend(), None, "20240603", "20240614",
            spec, load_library(), benchmark="000300.SH",
        )
        common = engine.bars_df.index.intersection(ds.index)
        assert len(common) > 0
        for col in ["mom20", "turnover_rate", "close_hfq"]:
            a = engine.bars_df.loc[common, col]
            b = ds.loc[common, col]
            assert (((a - b).abs().fillna(0) < 1e-9) | (a.isna() & b.isna())).all(), col

    def test_time_split_embargo(self):
        from btcore.ml.trainer import time_split_masks

        dates = pd.Series([f"202401{d:02d}" for d in range(1, 21)] * 3)
        train_mask, test_mask = time_split_masks(dates, horizon=3)
        train_dates = set(dates[train_mask])
        test_dates = set(dates[test_mask])
        assert not train_dates & test_dates
        uniq = sorted(dates.unique())
        boundary = uniq.index(min(test_dates))
        # 切点前 horizon 天被 embargo 剔除
        embargo = set(uniq[max(0, boundary - 3):boundary])
        assert not embargo & train_dates

    def test_metrics_perfect_prediction(self):
        from btcore.ml.metrics import daily_rank_ic, layered_returns, summarize_ic

        idx = pd.MultiIndex.from_tuples(
            [(d, s) for d in ["20240101", "20240102", "20240103"]
             for s in ["A", "B", "C", "D", "E", "F", "G", "H", "I", "J"]],
            names=["trade_date", "symbol"],
        )
        pred = pd.Series(np.tile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3), index=idx, dtype=float)
        fwd = pd.Series(np.tile(np.arange(0.01, 0.11, 0.01), 3), index=idx)
        ic = daily_rank_ic(pred, fwd)
        assert (ic > 0.9999).all()
        s = summarize_ic(ic)
        assert s["ic_mean"] == pytest.approx(1.0) and s["n_days"] == 3
        layered = layered_returns(pred, fwd, n_layers=5)
        assert layered["monotonic"] and layered["long_short"] == pytest.approx(0.08)

    def test_xs_forward_return_label(self):
        from btcore.ml.labels import xs_forward_return

        idx = pd.MultiIndex.from_tuples(
            [(d, s) for d in ["d1", "d2", "d3", "d4"] for s in ["A", "B"]],
            names=["trade_date", "symbol"],
        )
        panel = pd.DataFrame(
            {"close_hfq": [10, 20, 11, 22, 12.1, 26.4, 13.31, 29.04]},
            index=idx,
        )
        lab = xs_forward_return(panel, 1)
        # 末日无未来收益 → NaN
        assert lab.loc[("d4", "A"), "label"] != lab.loc[("d4", "A"), "label"]
        # d2→d3: A +10%, B +20% → B 排名高
        assert lab.loc[("d2", "B"), "label"] > lab.loc[("d2", "A"), "label"]
