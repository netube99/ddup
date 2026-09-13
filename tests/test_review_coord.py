"""F-ML-03 回归：raw-only panel 模型经 factor_specs 消费时加载期崩溃。

docs/ml_guide.md §2.2：panel 模型特征可以只含 raw 列（build_panel 显式支持），
factor_specs 引用 ml_<name> 时引擎应正常加载——策略闭包为空但模型列存在时
仍需挂空 FACTOR_NODES，Engine._build_factor_plan 只对 nodes is None 报错。
"""

import json

from btcore.engine import Engine
from btcore.strategy import Strategy
from btcore.strategy_loader import build_strategy


def _raw_only_model(tmp_path, name="a"):
    art = tmp_path / f"{name}.onnx"
    art.write_bytes(b"x")
    meta = {
        "version": 3,
        "name": name,
        "features": {"factors": [], "raw": ["close"]},
        "state_features": [],
        "post_transform": "none",
        "label": {"type": "xs_fwdret", "horizon": 5},
        "train_window": ["20240101", "20240630"],
        "scaler_mean": [0.0],
        "scaler_std": [1.0],
    }
    (tmp_path / f"{name}.meta.json").write_text(json.dumps(meta))
    return art


class _S(Strategy):
    def select(self, bars, snapshot, provider):
        return {}


def _build(tmp_path):
    art = _raw_only_model(tmp_path)
    return build_strategy(
        _S,
        {},
        factor_specs=[{"factor": "ml_a"}],
        models={"a": {"artifact": str(art), "meta": str(tmp_path / "a.meta.json")}},
        strategy_dir=str(tmp_path),
    )


def test_raw_only_panel_model_specs_attach_empty_nodes(tmp_path):
    strat = _build(tmp_path)
    assert strat.FACTOR_NODES == {}


def test_build_factor_plan_allows_empty_nodes_with_ml_specs(tmp_path):
    strat = _build(tmp_path)

    class _E:
        pass

    eng = _E()
    eng.strategy = strat
    plan = Engine._build_factor_plan(eng)
    assert plan["topo"] == []
    assert plan["main"] == set()
