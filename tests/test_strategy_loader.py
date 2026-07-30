"""btcore.strategy_loader 测试：YAML → Strategy 实例的加载与校验，
以及程序化 build_strategy 接口。"""

from types import SimpleNamespace

import pandas as pd
import pytest

from btcore.factors.library import resolve_spec
from btcore.strategy_loader import build_strategy, load_strategy
from btcore.strategy_tools import eval_factor_specs
from strategies.examples.rolling_ranker import RollingRanker

EXAMPLE_YAML = "strategies/examples/rolling_ranker/config.yaml"


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "s.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_load_example_yaml():
    strategy = load_strategy(EXAMPLE_YAML)
    assert isinstance(strategy, RollingRanker)
    assert strategy.config["top_k"] == 5
    assert strategy.config["conditions"]["stop_loss_pct"] == 0.06
    assert len(strategy.FACTOR_SPECS) == 2
    assert strategy.FILTER_RULES["exclude_st"] is True


def test_factor_specs_resolved_from_library():
    """factor: name 引用在加载期解析为 {name, weight, ascending}，
    因子定义（expr/where）进入 FACTOR_NODES 闭包。"""
    strategy = load_strategy(EXAMPLE_YAML)
    by_name = {s["name"]: s for s in strategy.FACTOR_SPECS}
    assert by_name["mom20"]["weight"] == 1.0
    assert by_name["low_turnover"]["ascending"] is True
    assert strategy.FACTOR_NODES["mom20"]["expr"] == "roc(close_hfq, 20)"
    assert strategy.FACTOR_NODES["low_turnover"]["expr"] == "turnover_rate"


def test_specs_and_rules_are_instance_attrs():
    """FACTOR_SPECS / FILTER_RULES 必须经 __init__ 注入实例，不污染类变量。"""
    s1 = load_strategy(EXAMPLE_YAML)
    s2 = RollingRanker(config={})
    assert s1.FACTOR_SPECS != s2.FACTOR_SPECS
    assert s2.FACTOR_SPECS == []
    assert RollingRanker.FILTER_RULES == {}


def test_missing_strategy_key(tmp_path):
    path = _write(tmp_path, "name: x\nconfig: {}\n")
    with pytest.raises(ValueError, match="strategy"):
        load_strategy(path)


def test_bad_class_path(tmp_path):
    path = _write(tmp_path, "strategy: btcore.strategy_loader:nope\n")
    with pytest.raises(ValueError, match="无法导入"):
        load_strategy(path)


def test_not_a_strategy_subclass(tmp_path):
    path = _write(tmp_path, "strategy: btcore.strategy_loader:load_strategy\n")
    with pytest.raises(ValueError, match="子类"):
        load_strategy(path)


def test_inline_expr_rejected(tmp_path):
    """factor_specs 只允许引用因子库名字，直写 expr 报错。"""
    path = _write(tmp_path, """\
strategy: strategies.examples.rolling_ranker:RollingRanker
factor_specs:
  - name: bad
    expr: "close_hfq"
""")
    with pytest.raises(ValueError, match="library.yaml"):
        load_strategy(path)


def test_unknown_factor_name(tmp_path):
    path = _write(tmp_path, """\
strategy: strategies.examples.rolling_ranker:RollingRanker
factor_specs:
  - factor: nope
""")
    with pytest.raises(ValueError, match="未知因子"):
        load_strategy(path)


def test_custom_factor_library(tmp_path):
    """factor_library 键指定自定义库（相对策略 YAML 目录解析）。"""
    (tmp_path / "my_lib.yaml").write_text(
        'factors:\n  my_factor:\n    expr: "close_hfq / pre_close"\n',
        encoding="utf-8",
    )
    path = _write(tmp_path, """\
strategy: strategies.examples.rolling_ranker:RollingRanker
factor_library: my_lib.yaml
factor_specs:
  - factor: my_factor
    weight: 2.0
""")
    strategy = load_strategy(path)
    spec = strategy.FACTOR_SPECS[0]
    assert spec["name"] == "my_factor"
    assert spec["weight"] == 2.0
    assert strategy.FACTOR_NODES["my_factor"]["expr"] == "close_hfq / pre_close"


def test_unknown_condition_key(tmp_path):
    path = _write(tmp_path, """\
strategy: strategies.examples.rolling_ranker:RollingRanker
conditions:
  martingale_pct: 0.5
""")
    with pytest.raises(ValueError, match="未知 conditions 键"):
        load_strategy(path)


def test_condition_out_of_range(tmp_path):
    path = _write(tmp_path, """\
strategy: strategies.examples.rolling_ranker:RollingRanker
conditions:
  stop_loss_pct: 1.5
""")
    with pytest.raises(ValueError, match="\\(0,1\\)"):
        load_strategy(path)


class _OwnUniverseStrategy(RollingRanker):
    """自定义 get_universe 的策略：loader 不应覆盖。"""

    def get_universe(self, provider, start, end):
        return ["000001.SZ"]


def _stub_provider(snapshots):
    def get_index_members(index_codes, start, end):
        return dict(snapshots)
    return SimpleNamespace(backend=SimpleNamespace(
        get_index_members=get_index_members))


_INDEX_YAML = """\
strategy: strategies.examples.rolling_ranker:RollingRanker
filter_rules:
  index_universe: ["000300.SH", "000905.SH"]
"""


class TestIndexUniverseLoading:
    _SNAPSHOTS = {"20240531": {"000001.SZ", "600036.SH"},
                  "20240628": {"600036.SH", "300750.SZ"}}

    def test_index_universe_is_known_key(self, tmp_path):
        strategy = load_strategy(_write(tmp_path, _INDEX_YAML))
        assert strategy.FILTER_RULES["index_universe"] == ["000300.SH", "000905.SH"]

    def test_default_get_universe_generated(self, tmp_path):
        """配置 index_universe 且策略未自定义 get_universe → loader 生成区间并集。"""
        strategy = load_strategy(_write(tmp_path, _INDEX_YAML))
        universe = strategy.get_universe(
            _stub_provider(self._SNAPSHOTS), "20240603", "20240630")
        assert universe == ["000001.SZ", "300750.SZ", "600036.SH"]

    def test_own_get_universe_not_overridden(self, tmp_path):
        path = _write(tmp_path, _INDEX_YAML.replace(
            "strategies.examples.rolling_ranker:RollingRanker",
            "tests.test_strategy_loader:_OwnUniverseStrategy",
        ))
        strategy = load_strategy(path)
        assert strategy.get_universe(
            _stub_provider(self._SNAPSHOTS), "20240603", "20240630") == ["000001.SZ"]

    def test_empty_snapshots_fall_back_to_full_market(self, tmp_path):
        """无成分数据时 get_universe 返回 None（全市场），与过滤层 fail-open 一致。"""
        strategy = load_strategy(_write(tmp_path, _INDEX_YAML))
        assert strategy.get_universe(
            _stub_provider({}), "20240603", "20240630") is None

    def test_missing_method_soft_fallback(self, tmp_path, caplog):
        """backend 未提供 get_index_members：告警一次，返回 None（不裁剪）。"""
        strategy = load_strategy(_write(tmp_path, _INDEX_YAML))
        provider = SimpleNamespace(backend=SimpleNamespace())
        assert strategy.get_universe(provider, "20240603", "20240630") is None
        assert strategy.get_universe(provider, "20240603", "20240630") is None
        assert caplog.text.count("白名单规则不生效") == 1


# ── build_strategy 程序化接口测试 ──


class TestBuildStrategy:
    def test_minimal(self):
        """仅 cls + config，无 factor/filter。"""
        strategy = build_strategy(RollingRanker, config={"top_k": 3})
        assert isinstance(strategy, RollingRanker)
        assert strategy.config["top_k"] == 3
        assert strategy.FACTOR_SPECS == []
        assert strategy.FACTOR_NODES is None
        assert strategy.FILTER_RULES == {}

    def test_full(self):
        """含 factor_specs / filter_rules。"""
        strategy = build_strategy(
            RollingRanker,
            config={"initial_capital": 500000, "top_k": 5,
                    "conditions": {"stop_loss_pct": 0.05}},
            factor_specs=[
                {"name": "mom20", "weight": 1.0, "ascending": False},
                {"name": "low_turnover", "weight": 0.5, "ascending": True},
            ],
            filter_rules={"exclude_st": True, "min_price": 5.0},
        )
        assert strategy.config["top_k"] == 5
        assert strategy.config["conditions"]["stop_loss_pct"] == 0.05
        assert len(strategy.FACTOR_SPECS) == 2
        by_name = {s["name"]: s for s in strategy.FACTOR_SPECS}
        assert by_name["mom20"]["weight"] == 1.0
        assert by_name["mom20"]["ascending"] is False
        assert by_name["low_turnover"]["ascending"] is True  # 库默认
        assert strategy.FACTOR_NODES["mom20"]["expr"] == "roc(close_hfq, 20)"
        assert strategy.FILTER_RULES["exclude_st"] is True

    def test_custom_library(self, tmp_path):
        """使用自定义因子库 dict。"""
        lib = {"my_factor": {"expr": "close_hfq / pre_close", "ascending": True}}
        strategy = build_strategy(
            RollingRanker,
            config={},
            factor_specs=[{"name": "my_factor", "weight": 2.0}],
            factor_library=lib,
        )
        spec = strategy.FACTOR_SPECS[0]
        assert spec["name"] == "my_factor"
        assert spec["weight"] == 2.0
        assert strategy.FACTOR_NODES["my_factor"]["expr"] == "close_hfq / pre_close"

    def test_unknown_factor(self):
        """factor_specs 引用不存在的因子应报错。"""
        with pytest.raises(ValueError, match="未知因子"):
            build_strategy(
                RollingRanker,
                config={},
                factor_specs=[{"name": "nope"}],
            )

    def test_specs_are_instance_attrs(self):
        """不污染类变量（与 YAML 路径等价）。"""
        s1 = build_strategy(
            RollingRanker,
            config={},
            factor_specs=[{"name": "mom20", "weight": 1.0}],
        )
        s2 = RollingRanker(config={})
        assert s1.FACTOR_SPECS != s2.FACTOR_SPECS
        assert s2.FACTOR_SPECS == []
        assert RollingRanker.FILTER_RULES == {}

    def test_equivalent_to_yaml(self):
        """build_strategy 与 load_strategy 构造的策略在关键属性上等价。"""
        from_yaml = load_strategy(EXAMPLE_YAML)
        from_dict = build_strategy(
            RollingRanker,
            config={
                "initial_capital": 1000000, "max_positions": 10, "top_k": 5,
                "cooldown_days": 3, "commission_rate": 0.0002,
                "stamp_tax_rate": 0.0005, "slippage_ticks": 2,
                "execution_price": "open",
                "conditions": {"stop_loss_pct": 0.06,
                                "take_profit_pct": 0.25,
                                "trailing_pct": 0.08},
            },
            factor_specs=[
                {"name": "mom20", "weight": 1.0, "ascending": False},
                {"name": "low_turnover", "weight": 0.5, "ascending": True},
            ],
            filter_rules={
                "exclude_st": True, "exclude_new_stock": True,
                "exclude_loss": True, "exclude_boards": ["BJ"],
                "min_price": 3.0,
            },
        )
        assert from_dict.config["top_k"] == from_yaml.config["top_k"]
        assert from_dict.FILTER_RULES == from_yaml.FILTER_RULES
        assert (from_dict.FACTOR_NODES["mom20"]["expr"]
                == from_yaml.FACTOR_NODES["mom20"]["expr"])
        assert len(from_dict.FACTOR_SPECS) == len(from_yaml.FACTOR_SPECS)

    def test_materialize_only_resolves(self):
        """spec 含 materialize_only: true → 解析后 materialize_only=True。"""
        spec = resolve_spec({"factor": "mom20", "materialize_only": True})
        assert spec["materialize_only"] is True
        assert spec["name"] == "mom20"
        assert spec["weight"] == 1.0

    def test_materialize_only_defaults_false(self):
        """未声明 materialize_only → 缺省 False。"""
        spec = resolve_spec({"factor": "mom20", "weight": 2.0})
        assert spec["materialize_only"] is False


# ── factor_universe 加载测试 ──


_FACTOR_UNIVERSE_YAML = """\
strategy: strategies.examples.rolling_ranker:RollingRanker
filter_rules:
  factor_universe: ["000300.SH", "000905.SH"]
"""


class _OwnFactorUniverseStrategy(RollingRanker):
    """自定义 get_factor_universe 的策略：loader 不应覆盖。"""

    def get_factor_universe(self, provider, start, end):
        return ["000001.SZ"]


class TestFactorUniverseLoading:
    _SNAPSHOTS = {"20240531": {"000001.SZ", "600036.SH"},
                  "20240628": {"600036.SH", "300750.SZ"}}

    def test_factor_universe_is_known_key(self, tmp_path):
        strategy = load_strategy(_write(tmp_path, _FACTOR_UNIVERSE_YAML))
        assert strategy.FILTER_RULES["factor_universe"] == ["000300.SH", "000905.SH"]

    def test_default_get_factor_universe_generated(self, tmp_path):
        """配置 factor_universe 且策略未自定义 → loader 生成区间并集。"""
        strategy = load_strategy(_write(tmp_path, _FACTOR_UNIVERSE_YAML))
        universe = strategy.get_factor_universe(
            _stub_provider(self._SNAPSHOTS), "20240603", "20240630")
        assert universe == ["000001.SZ", "300750.SZ", "600036.SH"]

    def test_own_get_factor_universe_not_overridden(self, tmp_path):
        path = _write(tmp_path, _FACTOR_UNIVERSE_YAML.replace(
            "strategies.examples.rolling_ranker:RollingRanker",
            "tests.test_strategy_loader:_OwnFactorUniverseStrategy",
        ))
        strategy = load_strategy(path)
        assert strategy.get_factor_universe(
            _stub_provider(self._SNAPSHOTS), "20240603", "20240630") == ["000001.SZ"]

    def test_no_factor_universe_falls_back_none(self, tmp_path):
        """未配置 factor_universe → get_factor_universe 保持基类默认 None。"""
        strategy = load_strategy(EXAMPLE_YAML)
        assert strategy.get_factor_universe(
            _stub_provider({}), "20240603", "20240630") is None

    def test_empty_snapshots_fall_back_none(self, tmp_path):
        """无成分数据时 get_factor_universe 返回 None（回退交易域）。"""
        strategy = load_strategy(_write(tmp_path, _FACTOR_UNIVERSE_YAML))
        assert strategy.get_factor_universe(
            _stub_provider({}), "20240603", "20240630") is None

    def test_missing_method_soft_fallback(self, tmp_path, caplog):
        """backend 未提供 get_index_members：告警一次，返回 None。"""
        strategy = load_strategy(_write(tmp_path, _FACTOR_UNIVERSE_YAML))
        provider = SimpleNamespace(backend=SimpleNamespace())
        assert strategy.get_factor_universe(provider, "20240603", "20240630") is None
        assert strategy.get_factor_universe(provider, "20240603", "20240630") is None
        assert caplog.text.count("因子计算域不生效") == 1


class TestBuildStrategyFactorUniverse:
    def test_build_strategy_with_factor_universe(self):
        """filter_rules 含 factor_universe 时正确挂接 get_factor_universe。"""
        strategy = build_strategy(
            RollingRanker,
            config={},
            filter_rules={"factor_universe": ["000300.SH"]},
        )
        assert strategy.FILTER_RULES["factor_universe"] == ["000300.SH"]
        # 无 backend 时 get_factor_universe 返回 None（软回退）
        provider = SimpleNamespace(backend=SimpleNamespace())
        assert strategy.get_factor_universe(provider, "20240603", "20240630") is None

    def test_build_strategy_factor_universe_absent(self):
        """未传 factor_universe → get_factor_universe 为 None。"""
        strategy = build_strategy(RollingRanker, config={})
        assert strategy.get_factor_universe(
            _stub_provider({}), "20240603", "20240630") is None


# ── eval_factor_specs materialize_only 测试 ──


class TestEvalFactorSpecsMaterializeOnly:
    def test_materialize_only_skipped_in_scoring_included_in_factor_df(self):
        """materialize_only 因子不参与加权得分但写入 factor_df。"""
        df = pd.DataFrame(
            {
                "mom20": [0.05, -0.02, 0.10, 0.03],
                "mkt_breadth20": [0.65, 0.65, 0.65, 0.65],
            },
            index=["A", "B", "C", "D"],
        )
        factor_specs = [
            {"name": "mom20", "weight": 1.0, "ascending": False},
            {"name": "mkt_breadth20", "weight": 1.0, "materialize_only": True},
        ]
        factor_df, score = eval_factor_specs(df, factor_specs)

        # factor_df 包含两列
        assert list(factor_df.columns) == ["mom20", "mkt_breadth20"]

        # score 仅由 mom20 决定（mkt_breadth20 被跳过）
        # mom20=[0.05,-0.02,0.10,0.03], ascending=False:
        # B(-0.02)→rank1 pct 0.25, D(0.03)→rank2 pct 0.5,
        # A(0.05)→rank3 pct 0.75, C(0.10)→rank4 pct 1.0
        assert score["A"] == 0.75
        assert score["C"] == 1.0
        assert score["B"] == 0.25

    def test_all_materialize_only_yields_uniform_score(self):
        """所有因子都是 materialize_only → score 全 1.0。"""
        df = pd.DataFrame(
            {"breadth": [0.5, 0.5]}, index=["A", "B"]
        )
        factor_specs = [
            {"name": "breadth", "weight": 1.0, "materialize_only": True},
        ]
        factor_df, score = eval_factor_specs(df, factor_specs)

        assert list(factor_df.columns) == ["breadth"]
        assert (score == 1.0).all()

    def test_mixed_scoring_and_materialize_only_weights_independent(self):
        """materialize_only 条目的 weight 不影响 total_weight 和得分。"""
        df = pd.DataFrame(
            {
                "a": [0.3, 0.1, 0.2],
                "b": [100, 200, 300],
            },
            index=["X", "Y", "Z"],
        )
        factor_specs = [
            {"name": "a", "weight": 0.5, "ascending": False},
            {"name": "b", "weight": 999.0, "materialize_only": True},
        ]
        factor_df, score = eval_factor_specs(df, factor_specs)

        # b 被跳过，score 仅由 a 决定
        # a=[0.3,0.1,0.2], ascending=False, rank(pct=True, ascending=True):
        # X(0.3)→rank 3→pct 3/3=1.0, Y(0.1)→rank 1→pct 1/3≈0.333, Z(0.2)→rank 2→pct 2/3≈0.667
        assert score["X"] == 1.0
        assert score["Z"] == pytest.approx(2.0 / 3.0)
        assert score["Y"] == pytest.approx(1.0 / 3.0)
        assert "b" in factor_df.columns
