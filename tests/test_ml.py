"""ML 子系统测试：spec 解析 / loader 整合 / runtime 推理 / 引擎双 scope / 训练同源。

ONNX 相关用例需要 onnxruntime + xgboost + onnxmltools（dev extra 已声明，
见 pyproject.toml）——缺失时仅相关用例跳过（_require_onnx），
spec/dataset/labels 等纯逻辑用例照常运行。
"""

import hashlib
import json
import logging
import sqlite3
import warnings

import numpy as np
import pandas as pd
import pytest

from btcore.engine import Engine
from btcore.ml import runtime as ml_runtime
from btcore.ml.spec import ModelSpec
from btcore.provider import DataProvider
from btcore.strategy import Strategy
from btcore.strategy_loader import build_strategy
from btcore.strategy_tools import (
    ConditionBuilder,
    bars_to_df,
    eval_factor_specs,
)
from tests.conftest import MockDataBackend

# ── 纯解析/整合用例（无 ONNX 依赖）──


def _write_meta(path, **over):
    meta = {
        "version": 3,
        "name": path.stem,
        "features": {"factors": ["mom20"], "raw": ["turnover_rate"]},
        "state_features": [],
        "post_transform": "xs_rank",
        "label": {"type": "xs_fwdret", "horizon": 5},
        "train_window": ["20240101", "20240630"],
    }
    meta.update(over)
    n_feat = (
        len(meta["features"]["factors"]) + len(meta["features"]["raw"])
        + len(meta["state_features"])
    )
    meta.setdefault("scaler_mean", [0.0] * n_feat)
    meta.setdefault("scaler_std", [1.0] * n_feat)
    path.write_text(json.dumps(meta))


def _trainer_scaler():
    """trainer 侧 scaler 实现（_fit_scaler/_scale，btcore 冻结中无公开 API）。

    两处私有符号集中在本函数引用——trainer 改名时只需更新这里。
    """
    from btcore.ml.trainer import _fit_scaler, _scale

    return _fit_scaler, _scale


def _assert_scaler_matches_reference(x, fit_scaler):
    """影子对照：_fit_scaler 输出 ≡ 文档契约参考实现（nanmean/nanstd、
    ±inf→NaN、全缺失/零方差列→0/1），捕获行为漂移而非仅改名。"""
    ref_x = np.where(np.isinf(x), np.nan, x)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        ref_mean = np.nanmean(ref_x, axis=0)
        ref_std = np.nanstd(ref_x, axis=0)
    ref_mean = np.where(np.isnan(ref_mean), 0.0, ref_mean)
    ref_std = np.where(np.isnan(ref_std) | (ref_std < 1e-10), 1.0, ref_std)
    mean, std = fit_scaler(x)
    np.testing.assert_allclose(mean, ref_mean, rtol=1e-12)
    np.testing.assert_allclose(std, ref_std, rtol=1e-12)


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

    def test_old_meta_v2_rejected(self, tmp_path):
        """v2 meta（ret_from_entry 旧口径）一律拒绝——必须重新训练。"""
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json", version=2)
        with pytest.raises(ValueError, match="需要 version=3"):
            ModelSpec.from_dict("a", {"artifact": "a.onnx"}, str(tmp_path))

    def test_scaler_dim_mismatch_rejected(self, tmp_path):
        """scaler 维度与特征契约不一致 = 静默错分，加载期 fail-fast。"""
        art = tmp_path / "a.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "a.meta.json", scaler_mean=[0.0, 0.0, 0.0])
        with pytest.raises(ValueError, match="scaler_mean 维度"):
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

    def test_model_exit_post_transform_warns(self, tmp_path, caplog):
        """model_exit 引用 post_transform != none 的模型时告警（阈值语义错位）。"""
        art = tmp_path / "m.onnx"
        art.write_bytes(b"x")
        _write_meta(tmp_path / "m.meta.json", state_features=["hold_days"],
                    post_transform="xs_rank")
        with caplog.at_level(logging.WARNING, logger="btcore.strategy_loader"):
            build_strategy(
                self._cls(),
                {"conditions": {"model_exit": [{"model": "m", "threshold": 0.6}]}},
                models={"m": {"artifact": str(art)}},
            )
        assert any("post_transform" in r.message for r in caplog.records)


# ── holding scope 标签重放 / scaler 口径（无 ONNX 依赖）──


class TestGuardSamplesBasis:
    def _spec(self):
        return ModelSpec(
            name="g", artifact="x.onnx",
            features=["mom20"], raw_features=["turnover_rate"],
            state_features=["hold_days"],
        )

    def test_trading_day_hold_days(self):
        """hold_days/dts 按市场交易日口径（跨周末场景下与日历日可区分）。"""
        from btcore.ml.labels import build_guard_samples

        idx = pd.MultiIndex.from_tuples(
            [(d, "A") for d in ["20240605", "20240606", "20240607", "20240610"]],
            names=["trade_date", "symbol"],
        )
        panel = pd.DataFrame(
            {"mom20": [1.0, np.nan, 3.0, 4.0], "turnover_rate": [0.5] * 4},
            index=idx,
        )
        pairs = pd.DataFrame([{
            "symbol": "A", "buy_date": "20240605", "sell_date": "20240610",
            "buy_price": 10.0, "pnl": -1.0, "trigger": "TREND_BREAK",
            "holding_days": 5,
        }])
        samples = build_guard_samples(panel, pairs, self._spec(), lookahead=2)
        # 成交当日 hd=1（引擎在成交日 compute_pending 已 +1），逐交易日 +1；
        # 日历日口径会跳过成交日且跨周末后漂移（0610 日历日 hd=5）
        assert samples["hold_days"].tolist() == [1.0, 2.0, 3.0, 4.0]
        # dts 同为交易日口径：0606/0607 距卖出 ∈ [1,2] → 正样本
        assert samples["label"].tolist() == [0, 1, 1, 0]
        # 缺失特征保留 NaN（trainer 在 scaler 之后填 0），不再预填 0.0
        assert np.isnan(samples["mom20"].iloc[1])

    def test_fit_scaler_nan_aware(self):
        """scaler 只在非缺失值上拟合；缺失在 scaler 之后填 0（= 训练均值）。"""
        fit_scaler, scale = _trainer_scaler()
        x = np.array([[1.0, np.nan], [3.0, 2.0], [5.0, 4.0]])
        mean, std = fit_scaler(x)
        assert mean[0] == pytest.approx(3.0)
        assert mean[1] == pytest.approx(3.0)  # nanmean([2, 4])
        scaled = scale(x, mean, std)
        assert scaled[0, 1] == 0.0  # 缺失 → 标准化空间 0 = 均值
        assert scaled[0, 0] == pytest.approx((1.0 - 3.0) / std[0])
        # 全缺失列回退 mean=0/std=1，不产生 NaN 参数
        mean2, std2 = fit_scaler(np.full((3, 1), np.nan))
        assert mean2[0] == 0.0 and std2[0] == 1.0
        # 影子对照：与文档契约参考实现逐值一致（行为漂移即碎）
        _assert_scaler_matches_reference(x, fit_scaler)

    def test_fit_scaler_inf_robust(self):
        """±inf 在 scaler 拟合前归一为 NaN，不污染 mean/std。"""
        fit_scaler, _ = _trainer_scaler()
        x = np.array([[1.0, np.inf], [3.0, 2.0], [5.0, np.nan]])
        mean, std = fit_scaler(x)
        assert np.isfinite(mean).all() and np.isfinite(std).all()
        assert mean[1] == pytest.approx(2.0)
        _assert_scaler_matches_reference(x, fit_scaler)

    def test_pit_training_domain(self):
        """PIT 训练域：每行取 ≤ 当日最近成分快照；训练域 = 引擎逐日计算域。"""
        from btcore.ml.dataset import apply_pit_membership

        idx = pd.MultiIndex.from_tuples(
            [(d, s) for d in ["20240603", "20240604", "20240605"]
             for s in ["A", "B", "C"]],
            names=["trade_date", "symbol"],
        )
        panel = pd.DataFrame({"mom20": [1.0] * 9}, index=idx)
        members = {"20240603": {"A", "B"}, "20240605": {"A", "B", "C"}}
        out = apply_pit_membership(panel, members)
        # 0603/0604 按 0603 快照（C 不在）；0605 按当日快照（C 在）
        assert set(out.loc["20240603"].index) == {"A", "B"}
        assert set(out.loc["20240604"].index) == {"A", "B"}
        assert set(out.loc["20240605"].index) == {"A", "B", "C"}
        # 无成员配置 → 原样返回
        assert len(apply_pit_membership(panel, None)) == len(panel)


class TestTradePairRounds:
    """extract_trade_pairs 回合语义：不限买入 trigger、多买多卖、残缺跳过。"""

    @staticmethod
    def _insert_trade(conn, run_id, row):
        date, sym, side, trig, price, shares = row[:6]
        # 显式 net（红利/审计行）优先；缺省按无费用买卖净额
        net = row[6] if len(row) > 6 else price * shares * (1 if side == "SELL" else -1)
        conn.execute(
            "INSERT INTO trade_log (run_id, date, symbol, side, trigger,"
            " price, shares, turnover, commission, net_amount)"
            " VALUES (?,?,?,?,?,?,?,0,0,?)",
            (run_id, date, sym, side, trig, price, shares, net),
        )

    @classmethod
    def _db(cls, tmp_path, rows):
        """单 completed run 结果库；rows = (date, symbol, side, trigger, price, shares[, net])。"""
        from btcore import database

        p = tmp_path / "trades.db"
        conn = database.init_backtest_db(str(p))
        run_id = database.write_run(
            conn, created_at="2024-01-01", strategy="t", start_date="20240101",
            end_date="20240201", initial_capital=1e6, config_json="{}",
            status="completed",
        )
        for row in rows:
            cls._insert_trade(conn, run_id, row)
        conn.commit()
        conn.close()
        return str(p)

    @classmethod
    def _multi_db(cls, tmp_path, run_statuses, trades):
        """多 run 结果库；trades 首元素为 run_statuses 的 0 基下标，其余同 _db。
        返回 (db_path, run_ids)。"""
        from btcore import database

        p = tmp_path / "multi.db"
        conn = database.init_backtest_db(str(p))
        run_ids = [
            database.write_run(
                conn, created_at="2024-01-01", strategy="t",
                start_date="20240101", end_date="20240201",
                initial_capital=1e6, config_json="{}", status=status,
            )
            for status in run_statuses
        ]
        for row in trades:
            cls._insert_trade(conn, run_ids[row[0]], row[1:])
        conn.commit()
        conn.close()
        return str(p), run_ids

    def test_target_buy_round_paired(self, tmp_path):
        """TARGET 买入回合不再静默蒸发。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "TARGET", 10.0, 100),
            ("20240110", "X", "SELL", "TREND_BREAK", 9.0, 100),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        r = pairs.iloc[0]
        assert r["trigger"] == "TREND_BREAK" and r["pnl"] == -100.0

    def test_condition_buy_round_paired(self, tmp_path):
        """条件买入回合（AGENTS.md 推荐触发范式）同样计入。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "BREAKOUT_BUY", 10.0, 100),
            ("20240110", "X", "SELL", "TREND_BREAK", 9.0, 100),
        ])
        assert len(extract_trade_pairs(db)) == 1

    def test_stk_div_round_paired(self, tmp_path):
        """送转增股回合不再因"超卖"被丢弃，buy_price 为除权后每股成本。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100),
            ("20240105", "X", "STK_DIV", "CORPORATE", 0.0, 140),
            ("20240108", "X", "SELL", "MANUAL", 5.1, 140),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        r = pairs.iloc[0]
        assert r["buy_price"] == pytest.approx(1000.0 / 140, abs=1e-4)
        assert r["pnl"] == pytest.approx(140 * 5.1 - 100 * 10.0)

    def test_partial_sells_one_round(self, tmp_path):
        """买 1000 两次部分卖 500/500 → 1 回合，pnl 为两笔合计，trigger 取末笔。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 1000),
            ("20240105", "X", "SELL", "MANUAL", 9.0, 500),
            ("20240110", "X", "SELL", "TREND_BREAK", 8.0, 500),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        r = pairs.iloc[0]
        assert r["pnl"] == -1500.0  # 9000 + 4000 - 10000（费用不计）
        assert r["trigger"] == "TREND_BREAK"  # 末笔卖出
        assert r["buy_date"] == "20240102" and r["sell_date"] == "20240110"

    def test_pyramiding_weighted_avg_price(self, tmp_path):
        """回合内多次买入：buy_price 为股数加权均价。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100),
            ("20240103", "X", "BUY", "MANUAL", 12.0, 100),
            ("20240110", "X", "SELL", "MANUAL", 11.0, 200),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        assert pairs.iloc[0]["buy_price"] == pytest.approx(11.0)
        assert pairs.iloc[0]["pnl"] == pytest.approx(0.0)

    def test_dangling_sell_skipped_with_warning(self, tmp_path, caplog):
        """卖无买：跳过并告警（静默丢弃会产出错误标签）。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "SELL", "MANUAL", 9.0, 100),
        ])
        with caplog.at_level(logging.WARNING, logger="btcore.ml.labels"):
            pairs = extract_trade_pairs(db)
        assert len(pairs) == 0
        assert any("卖出无对应买入" in r.message for r in caplog.records)

    def test_oversell_round_skipped(self, tmp_path, caplog):
        """卖出超过买入股数：残缺回合跳过并告警。"""
        # 该场景在送转落库后仅剩真正的"超卖"（无送转支撑）才触发
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100),
            ("20240110", "X", "SELL", "MANUAL", 9.0, 150),
        ])
        with caplog.at_level(logging.WARNING, logger="btcore.ml.labels"):
            pairs = extract_trade_pairs(db)
        assert len(pairs) == 0
        assert any("卖出股数超过买入" in r.message for r in caplog.records)

    def test_run_id_filter_isolates_runs(self, tmp_path):
        """多 run 同库：run_id 过滤不混入（同 symbol 交错交易只配对指定 run）。"""
        from btcore.ml.labels import extract_trade_pairs

        db, run_ids = self._multi_db(tmp_path, ["completed", "completed"], [
            (0, "20240102", "X", "BUY", "MANUAL", 10.0, 100),
            (1, "20240103", "X", "BUY", "MANUAL", 20.0, 100),
            (0, "20240110", "X", "SELL", "TREND_BREAK", 9.0, 100),
            (1, "20240111", "X", "SELL", "MANUAL", 22.0, 100),
        ])
        pairs = extract_trade_pairs(db, run_id=run_ids[0])
        assert len(pairs) == 1
        r = pairs.iloc[0]
        assert r["buy_price"] == pytest.approx(10.0)
        assert r["pnl"] == -100.0  # 900 - 1000，不含 run2 的 ±2000
        pairs2 = extract_trade_pairs(db, run_id=run_ids[1])
        assert len(pairs2) == 1
        assert pairs2.iloc[0]["pnl"] == 200.0

    def test_default_run_id_latest_completed_with_warning(self, tmp_path, caplog):
        """run_id=None：多 run 时取最新 completed（running 不算）并告警所选。"""
        from btcore.ml.labels import extract_trade_pairs

        db, run_ids = self._multi_db(tmp_path, ["completed", "running"], [
            (0, "20240102", "X", "BUY", "MANUAL", 10.0, 100),
            (0, "20240110", "X", "SELL", "MANUAL", 11.0, 100),
            (1, "20240102", "Y", "BUY", "MANUAL", 5.0, 100),
        ])
        with caplog.at_level(logging.WARNING, logger="btcore.ml.labels"):
            pairs = extract_trade_pairs(db)
        assert len(pairs) == 1 and pairs.iloc[0]["symbol"] == "X"
        assert any(
            f"run_id={run_ids[0]}" in r.message for r in caplog.records
        )

    def test_missing_runs_table_fails_fast(self, tmp_path):
        """无 runs 表的库无法定位 run：明确报错而非静默混入全部交易。"""
        from btcore.ml.labels import extract_trade_pairs

        p = tmp_path / "legacy.db"
        conn = sqlite3.connect(p)
        conn.execute(
            "CREATE TABLE trade_log (id INTEGER PRIMARY KEY AUTOINCREMENT,"
            " date TEXT, symbol TEXT, side TEXT, trigger TEXT, price REAL,"
            " shares INTEGER, net_amount REAL)"
        )
        conn.close()
        with pytest.raises(ValueError, match="runs"):
            extract_trade_pairs(str(p))

    def test_same_day_div_before_sell_includes_dividend(self, tmp_path, caplog):
        """同日 DIV+SELL：DIV 落库 id 晚于 SELL 也按盘前口径先处理，
        红利计入回合 pnl 且无残缺告警。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100),
            # 引擎落库序：SELL（结算）先于 DIV（公司行为），id 更小
            ("20240110", "X", "SELL", "TREND_BREAK", 9.0, 100),
            ("20240110", "X", "DIV", "CORPORATE", 0.0, 0, 50.0),
        ])
        with caplog.at_level(logging.WARNING, logger="btcore.ml.labels"):
            pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        assert pairs.iloc[0]["pnl"] == -50.0  # 900 + 50 - 1000
        assert not caplog.records

    def test_same_day_stk_div_before_sell_not_oversold(self, tmp_path, caplog):
        """同日 STK_DIV+SELL：送转落库晚于卖出也不误判超卖丢弃。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100),
            ("20240110", "X", "SELL", "TREND_BREAK", 7.0, 140),
            ("20240110", "X", "STK_DIV", "CORPORATE", 0.0, 140),
        ])
        with caplog.at_level(logging.WARNING, logger="btcore.ml.labels"):
            pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        r = pairs.iloc[0]
        assert r["pnl"] == pytest.approx(7.0 * 140 - 1000.0)
        assert r["buy_price"] == pytest.approx(1000.0 / 140, abs=1e-4)
        assert not any("超卖" in rec.message for rec in caplog.records)

    def test_adjust_rows_silently_skipped(self, tmp_path, caplog):
        """实盘账本 ADJUST 现金审计行：不进回合、不进 pnl、无告警。"""
        from btcore.ml.labels import extract_trade_pairs

        db = self._db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100),
            ("20240105", "", "ADJUST", "MANUAL", 0.0, 0, -500.0),
            ("20240110", "X", "SELL", "TREND_BREAK", 9.0, 100),
        ])
        with caplog.at_level(logging.WARNING, logger="btcore.ml.labels"):
            pairs = extract_trade_pairs(db)
        assert len(pairs) == 1
        assert pairs.iloc[0]["pnl"] == -100.0
        assert not caplog.records


# ── ONNX 相关用例 ──
#
# 依赖（onnxruntime / xgboost / onnxmltools）在真正需要处做函数/类级
# importorskip（_require_onnx），不再模块级整组跳过。

def _require_onnx():
    """ONNX 全链路依赖缺失时跳过当前用例（在 _make_model 内调用）。"""
    pytest.importorskip("onnxruntime", reason="需要 onnxruntime")
    pytest.importorskip("xgboost", reason="需要 xgboost")
    pytest.importorskip("onnxmltools", reason="需要 onnxmltools")


def _make_model(tmp_path, name="m", n_features=2, state=(), post="none"):
    """训练一个可复现的小模型并导出 ONNX + meta v3。"""
    _require_onnx()
    from onnxmltools import convert_xgboost
    from onnxmltools.convert.common.data_types import FloatTensorType
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

    def test_ret_from_entry_raw_price_basis(self):
        """ret_from_entry 裸价口径：复权因子/除权日不产生假信号。"""
        from tests.conftest import make_bar, make_holding

        h = make_holding(entry_price=10.0, holding_days=5)
        # adj=3（历史分红）走平市场：旧 hfq 口径 +200%，裸价口径应为 0
        st = ml_runtime.compute_state_features(
            ["ret_from_entry"], make_bar(close=10.0, close_hfq=30.0), h,
        )
        assert st["ret_from_entry"] == pytest.approx(0.0)
        # 除权日裸价不动 → 特征不变（hfq 口径会跳变 2.0 → 1.85）
        st2 = ml_runtime.compute_state_features(
            ["ret_from_entry"], make_bar(close=10.0, close_hfq=28.5), h,
        )
        assert st2["ret_from_entry"] == pytest.approx(0.0)
        # 缺 close_hfq 列时口径不再翻转
        st3 = ml_runtime.compute_state_features(
            ["ret_from_entry"], {"close": 10.0}, h,
        )
        assert st3["ret_from_entry"] == pytest.approx(0.0)
        # 真实收益仍正确
        st4 = ml_runtime.compute_state_features(
            ["ret_from_entry"], make_bar(close=12.0, close_hfq=36.0), h,
        )
        assert st4["ret_from_entry"] == pytest.approx(0.2)

    def test_holding_scores_batch_matches_single(self, tmp_path):
        """批量推理与逐持仓单行推理逐值一致，缺失护栏同行为。"""
        art = _make_model(
            tmp_path, name="g", n_features=4,
            state=("hold_days", "ret_from_entry"),
        )
        spec = ModelSpec.from_dict("g", {"artifact": str(art)}, str(tmp_path))
        from tests.conftest import make_bar, make_holding

        bars = [
            make_bar(close=11.0, close_hfq=11.0, mom20=1.0, turnover_rate=1.0),
            make_bar(close=10.5, close_hfq=10.5, mom20=0.5, turnover_rate=0.8),
        ]
        holds = [
            make_holding(entry_price=10.0, holding_days=5),
            make_holding(entry_price=9.0, holding_days=2),
        ]
        batch = ml_runtime.holding_scores_batch(spec, bars, holds)
        single = [
            ml_runtime.holding_score(spec, b, h) for b, h in zip(bars, holds)
        ]
        for a, b in zip(batch, single):
            if a is None or b is None:
                assert a is None and b is None
            else:
                assert a == pytest.approx(b, abs=1e-9)
        # 缺失过半的行在批量路径同样返回 None（3/4 缺失，模型输入 4 维）
        art2 = tmp_path / "g2.onnx"
        art2.write_bytes(art.read_bytes())
        _write_meta(
            tmp_path / "g2.meta.json", name="g2",
            features={"factors": ["mom20"],
                      "raw": ["turnover_rate", "pe_ttm"]},
            state_features=["hold_days"], post_transform="none",
        )
        spec2 = ModelSpec.from_dict("g2", {"artifact": str(art2)}, str(tmp_path))
        bad_bar = {"trade_date": "20240603"}
        assert ml_runtime.holding_scores_batch(spec2, [bad_bar], holds[0:1]) == [None]

    def test_inf_inputs_treated_missing(self, tmp_path):
        """±inf（除零因子）在推理侧归一为 0（= 缺失中性），不产生发散分数。"""
        art = _make_model(tmp_path)
        spec = ModelSpec.from_dict("m", {"artifact": str(art)}, str(tmp_path))
        out = ml_runtime._run_batch(spec, np.array([[np.inf, -np.inf]]))
        assert np.isfinite(out).all()

    def test_flat_post_transform(self):
        s = pd.Series({"A": 1.0, "B": 3.0, "C": 2.0})
        ranked = ml_runtime.apply_post_transform_flat(s, "xs_rank")
        assert ranked["B"] == 1.0 and ranked["A"] == pytest.approx(1 / 3)
        z = ml_runtime.apply_post_transform_flat(s, "xs_zscore")
        assert z.mean() == pytest.approx(0.0, abs=1e-9)

    def test_missing_neutral_after_scaler(self, tmp_path):
        """缺失值在 scaler 之后填 0：NaN 行与"恰为训练均值"行得分相同。"""
        art = _make_model(tmp_path)
        _write_meta(  # 非平凡 scaler：原始空间的 0 不再是均值
            tmp_path / "m.meta.json",
            scaler_mean=[3.0, 1.0], scaler_std=[2.0, 1.0],
        )
        spec = ModelSpec.from_dict("m", {"artifact": str(art)}, str(tmp_path))
        at_mean = ml_runtime._run_batch(spec, np.array([[3.0, 1.0]]))
        missing = ml_runtime._run_batch(spec, np.array([[np.nan, np.nan]]))
        assert float(missing[0]) == pytest.approx(float(at_mean[0]))

    def test_materialize_missing_ratio_guard(self, tmp_path):
        """panel 路径与 holding 同一护栏：特征缺失过半的行无分数（NaN）。"""
        art = _make_model(tmp_path, post="none")
        spec = ModelSpec.from_dict("m", {"artifact": str(art)}, str(tmp_path))
        idx = pd.MultiIndex.from_tuples(
            [("20240603", "FULL"), ("20240603", "HALF"), ("20240603", "NONE")],
            names=["trade_date", "symbol"],
        )
        df = pd.DataFrame(
            {"mom20": [1.0, 1.0, np.nan], "turnover_rate": [1.0, np.nan, np.nan]},
            index=idx,
        )
        ml_runtime.materialize_predictions(df, [spec])
        assert not np.isnan(df.loc[("20240603", "FULL"), "ml_m"])
        assert not np.isnan(df.loc[("20240603", "HALF"), "ml_m"])  # 恰好 50% 不越界
        assert np.isnan(df.loc[("20240603", "NONE"), "ml_m"])


class _BuyThenSell(Strategy):
    """首日买一只，holding_days 到阈值后卖出（制造跨周末持仓回合）。"""

    def on_start(self, provider, first_date, end_date=None): pass

    def select(self, bars, snapshot, provider):
        if not snapshot.holdings:
            if getattr(self, "_bought", False):
                return {"buy": [], "sell": []}
            self._bought = True
            return {"buy": [sorted(bars)[0]], "sell": []}
        sym = next(iter(snapshot.holdings))
        if snapshot.holdings[sym].holding_days >= 6:
            return {"buy": [], "sell": [sym]}
        return {"buy": [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


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

    def test_train_window_overlap_warns(self, tmp_path, caplog):
        """回测窗口与模型训练窗口重叠 → 告警；完全不重叠 → 无告警。"""
        art = _make_model(tmp_path)  # meta train_window=["20240101","20240630"]

        def _engine(db_name):
            strat = build_strategy(
                _TopK,
                {"initial_capital": 1_000_000, "max_positions": 3},
                factor_specs=[{"factor": "ml_m", "weight": 1.0}],
                models={"m": {"artifact": str(art)}},
            )
            return Engine(strat, DataProvider(MockDataBackend()),
                          db_path=str(tmp_path / db_name))

        with caplog.at_level(logging.WARNING, logger="btcore.engine"):
            _engine("w1.db").run("20240603", "20240614")
        assert any("样本内乐观偏差" in r.message for r in caplog.records)

        caplog.clear()
        _write_meta(
            tmp_path / "m.meta.json", train_window=["20230101", "20231231"],
            artifact_sha256=hashlib.sha256(art.read_bytes()).hexdigest(),
        )
        with caplog.at_level(logging.WARNING, logger="btcore.engine"):
            _engine("w2.db").run("20240603", "20240614")
        assert not any("样本内乐观偏差" in r.message for r in caplog.records)


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

    def test_guard_samples_match_engine_holding_days(self, tmp_path):
        """holding 侧同源验收：标签重放的 hold_days 与引擎决策时点的
        holding_days（debug 快照）逐日一致——日历日口径会跨周末漂移。"""
        from btcore.factors.library import load_library
        from btcore.ml import dataset, labels

        strat = build_strategy(
            _BuyThenSell, {"initial_capital": 1_000_000, "max_positions": 3},
        )
        db = str(tmp_path / "hd.db")
        engine = Engine(strat, DataProvider(MockDataBackend()), db_path=db,
                        debug=True)
        engine.run("20240603", "20240614")

        pairs = labels.extract_trade_pairs(db)
        assert len(pairs) == 1
        sym = pairs.iloc[0]["symbol"]

        spec = ModelSpec(
            name="g", artifact="x.onnx",
            features=["mom20"], raw_features=["turnover_rate"],
            state_features=["hold_days"],
        )
        panel = dataset.build_panel(
            MockDataBackend(), [sym], "20240603", "20240614",
            spec, load_library(), benchmark="000300.SH",
        )
        samples = labels.build_guard_samples(panel, pairs, spec, lookahead=0)

        conn = sqlite3.connect(db)
        snaps = {}
        for d, js in conn.execute("SELECT date, snapshot_json FROM debug_snapshots"):
            h = json.loads(js)["holdings_detail"].get(sym)
            if h:
                snaps[d] = h["holding_days"]
        conn.close()

        # 跨周末持仓（0604 成交 ~ 0611）：日历日口径在 0610 之后会偏大
        assert len(samples) >= 5 and samples["hold_days"].max() >= 5
        for row in samples.itertuples():
            assert row.hold_days == snaps[row.trade_date], row.trade_date

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
