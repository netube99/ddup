"""TDD review tests: engine / provider / strategy / strategy_loader correctness.

Scope under review: btcore/engine.py, provider.py, strategy.py, strategy_loader.py.
Each test asserts documented-correct behavior (docs/strategy_guide.md,
build_strategy docstring, Strategy class docstring).
"""

from btcore.strategy import Strategy
from btcore.strategy_loader import build_strategy, load_strategy


class ClassDefaultStrategy(Strategy):
    """类级别声明式默认值策略。

    docs/strategy_guide.md §3.1: "类变量 —— 声明式默认值，YAML 可覆盖"；
    build_strategy docstring: factor_specs "不传时用类默认值"；
    Strategy 类 docstring: "子类可以在类级别定义默认值，也可以在构造时
    通过 factor_specs / filter_rules 传入实例级覆盖"。
    """

    FILTER_RULES = {"min_price": 5.0}
    FACTOR_SPECS = [{"name": "mom20", "weight": 1.0}]

    def select(self, bars, account_snapshot, provider):
        return {"buy": [], "sell": []}


def test_build_strategy_preserves_class_default_filter_rules():
    """不传 filter_rules → 类默认 FILTER_RULES 生效（而非被清空）。"""
    strategy = build_strategy(ClassDefaultStrategy, config={})
    assert strategy.FILTER_RULES == {"min_price": 5.0}


def test_build_strategy_preserves_class_default_factor_specs():
    """不传 factor_specs → 类默认因子引用被解析并挂接 FACTOR_NODES 闭包。"""
    strategy = build_strategy(ClassDefaultStrategy, config={})
    assert [spec["name"] for spec in strategy.FACTOR_SPECS] == ["mom20"]
    assert strategy.FACTOR_NODES["mom20"]["expr"] == "roc(close_hfq, 20)"


def test_build_strategy_explicit_override_beats_class_defaults():
    """显式传入的 filter_rules / factor_specs 覆盖类默认值。"""
    strategy = build_strategy(
        ClassDefaultStrategy, config={}, filter_rules={"min_price": 1.0}
    )
    assert strategy.FILTER_RULES == {"min_price": 1.0}


def test_load_strategy_yaml_absent_keys_fall_back_to_class_defaults(tmp_path):
    """YAML 路径等价：缺 filter_rules / factor_specs 键时回退类默认值。"""
    path = tmp_path / "s.yaml"
    path.write_text(
        "strategy: tests.test_review_engine:ClassDefaultStrategy\nconfig: {}\n",
        encoding="utf-8",
    )
    strategy = load_strategy(str(path))
    assert strategy.FILTER_RULES == {"min_price": 5.0}
    assert [spec["name"] for spec in strategy.FACTOR_SPECS] == ["mom20"]
    assert strategy.FACTOR_NODES["mom20"]["expr"] == "roc(close_hfq, 20)"


# ── 显式空值覆盖语义（V-ENG-02 钉住回归）──
# docs/strategy_guide.md §3.1: 类变量是声明式默认值，YAML/显式传参可覆盖；
# 显式空 list/dict 是覆盖（清空类默认），与缺省键（None → 回退类默认）不同。
# 此前该行为仅由 probe 证据支撑，无测试钉住。


def test_build_strategy_explicit_empty_overrides_class_defaults():
    """程序化路径：factor_specs=[] / filter_rules={} 显式清空类默认值。"""
    strategy = build_strategy(
        ClassDefaultStrategy, config={}, factor_specs=[], filter_rules={}
    )
    assert strategy.FACTOR_SPECS == []
    assert strategy.FILTER_RULES == {}
    # 空 specs 不挂 FACTOR_NODES（引擎按无因子计划处理）
    assert getattr(strategy, "FACTOR_NODES", None) is None


def test_load_strategy_yaml_explicit_empty_overrides_class_defaults(tmp_path):
    """YAML 路径等价：显式空键覆盖，缺键回退。"""
    path = tmp_path / "s.yaml"
    path.write_text(
        "strategy: tests.test_review_engine:ClassDefaultStrategy\n"
        "config: {}\n"
        "factor_specs: []\n"
        "filter_rules: {}\n",
        encoding="utf-8",
    )
    strategy = load_strategy(str(path))
    assert strategy.FACTOR_SPECS == []
    assert strategy.FILTER_RULES == {}
