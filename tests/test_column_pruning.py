"""preload 列裁剪：required_bar_columns 静态推导 + query_bars columns 契约 + e2e 等价。"""

import pytest

from btcore.engine import Engine, required_bar_columns
from btcore.factors.plan import REQUIRED_BAR_COLUMNS, build_factor_plan
from btcore.provider import DataProvider
from strategies.examples.rolling_ranker import RollingRanker
from tests.conftest import MockDataBackend

_NODES = {
    "mom20": {"expr": "roc(close_hfq, 20)"},
    "low_turnover": {"expr": "turnover_rate"},
    "value": {"expr": "dv_ttm / pb", "where": "pb > 0"},
}
_SPECS = [
    {"name": "mom20", "weight": 1.0, "ascending": False},
    {"name": "low_turnover", "weight": 0.5, "ascending": True},
    {"name": "value", "weight": 1.0, "ascending": False},
]


def _strategy(nodes=None, **kw) -> RollingRanker:
    s = RollingRanker(
        config={"top_k": 3, "max_positions": 3, **kw.pop("config", {})},
        factor_specs=kw.pop("factor_specs", list(_SPECS)),
        filter_rules=kw.pop(
            "filter_rules",
            {"exclude_st": True, "exclude_new_stock": True,
             "exclude_loss": True, "min_price": 3.0},
        ),
        **kw,
    )
    nodes = _NODES if nodes is None else nodes
    if nodes is not None and s.FACTOR_SPECS:
        s.FACTOR_NODES = nodes
    return s


def _plan(strategy):
    return build_factor_plan(
        strategy.FACTOR_NODES, [s["name"] for s in strategy.FACTOR_SPECS]
    )


class TestDerivation:
    def test_required_plus_factor_and_filter_columns(self):
        s = _strategy()
        cols = required_bar_columns(s, _plan(s))
        # 必需 10 列 + 因子引用(turnover_rate/dv_ttm/where 的 pb)
        # + exclude_loss 的 eps/pe_ttm；close_hfq 展开为基础列（已在必需列里）
        assert set(cols) == set(REQUIRED_BAR_COLUMNS) | {
            "turnover_rate", "dv_ttm", "pb", "eps", "pe_ttm",
        }

    def test_derived_columns_excluded(self):
        """引擎可精确派生的 *_hfq / pct_chg 不进入请求列（展开为基础列）。"""
        nodes = {"x": {"expr": "close_hfq / open_hfq"}}
        s = _strategy(nodes=nodes, factor_specs=[{"name": "x"}])
        cols = required_bar_columns(s, _plan(s))
        assert "close_hfq" not in cols
        assert "open_hfq" not in cols

    def test_materialized_factor_columns_not_requested(self):
        """物化因子列（mom20/value）不向 backend 请求。"""
        s = _strategy()
        cols = required_bar_columns(s, _plan(s))
        assert "mom20" not in cols and "value" not in cols

    def test_exclude_loss_only_when_explicit(self):
        """exclude_loss 显式开启才要求 eps/pe_ttm（缺省 True 是 StockFilter 运行时语义，
        不用 StockFilter 的策略不应被强制要求该列）。"""
        rules = {"exclude_st": True}
        s = _strategy(filter_rules=rules)
        cols = required_bar_columns(s, _plan(s))
        assert "pe_ttm" not in cols
        assert "eps" not in cols
        s = _strategy(filter_rules={**rules, "exclude_loss": True})
        cols = required_bar_columns(s, _plan(s))
        assert "pe_ttm" in cols
        assert "eps" in cols

    def test_required_fields_included(self):
        """select() 命令式访问的列经 REQUIRED_FIELDS 声明后进入请求列。"""
        s = _strategy()
        s.REQUIRED_FIELDS = [*s.REQUIRED_FIELDS, "sentiment_score"]
        assert "sentiment_score" in required_bar_columns(s, _plan(s))

    def test_specs_without_nodes_raise(self):
        """FACTOR_SPECS 无 FACTOR_NODES → preload 报错（提示经 loader 加载）。"""
        s = _strategy(nodes=None)
        s.FACTOR_NODES = None
        engine = Engine(s, DataProvider(MockDataBackend()),
                        initial_capital=1_000_000, db_path=":memory:")
        with pytest.raises(ValueError, match="FACTOR_NODES"):
            engine.run("20240603", "20240628")

    def test_no_declarations_loads_required_only(self):
        class Bare:
            pass
        assert required_bar_columns(Bare()) == sorted(REQUIRED_BAR_COLUMNS)


class TestBackendColumnsContract:
    def test_mock_query_bars_column_subset(self):
        backend = MockDataBackend()
        df = backend.query_bars(None, "20240603", "20240628",
                                columns=["open", "close"])
        assert sorted(df.columns) == ["close", "open"]

    def test_mock_query_bars_unknown_column_raises(self):
        backend = MockDataBackend()
        with pytest.raises(ValueError, match="未知列名"):
            backend.query_bars(None, "20240603", "20240628",
                               columns=["open", "nope"])

    def test_mock_query_bars_default_unchanged(self):
        """缺省列 = bars 契约列 ∪ LEFT JOIN 的 aux 表列（精确集合，防止静默增删）。"""
        import os

        import pandas as pd

        from tests.conftest import _AUX_TABLES, FIXTURES_DIR

        backend = MockDataBackend()
        df = backend.query_bars(None, "20240603", "20240628")
        expected = set(pd.read_parquet(
            os.path.join(FIXTURES_DIR, "bars.parquet")).columns)
        for t in _AUX_TABLES + ["limits"]:
            aux = pd.read_parquet(os.path.join(FIXTURES_DIR, f"{t}.parquet"))
            expected |= set(aux.columns) - {"ts_code"}  # ts_code 重命名为 symbol
        # 索引键（trade_date/symbol）不进列
        expected -= {"trade_date", "symbol"}
        assert set(df.columns) == expected


def _run(strategy, monkeypatch=None, full_columns=False) -> dict:
    if full_columns:
        monkeypatch.setattr(
            "btcore.engine.required_bar_columns", lambda s, fplan=None: None
        )
    engine = Engine(strategy, DataProvider(MockDataBackend()),
                    initial_capital=1_000_000, db_path=":memory:")
    return engine.run("20240603", "20240628")


def _normalize(trade_log) -> list[tuple]:
    return sorted(
        (r.date, r.symbol, r.side, r.shares, round(r.price, 4))
        for r in trade_log.itertuples()
    )


class TestEndToEnd:
    def test_pruned_columns_loaded(self):
        """engine.bars_df = 推导列子集 + 派生列 + 物化因子列。"""
        engine = Engine(_strategy(), DataProvider(MockDataBackend()),
                        initial_capital=1_000_000, db_path=":memory:")
        engine.run("20240603", "20240628")
        expected = set(REQUIRED_BAR_COLUMNS) | {
            "turnover_rate", "dv_ttm", "pb", "eps", "pe_ttm",
            # 引擎派生列
            "open_hfq", "high_hfq", "low_hfq", "close_hfq", "pct_chg",
            # 物化因子列
            "mom20", "low_turnover", "value",
        }
        assert set(engine.bars_df.columns) == expected

    def test_pruned_matches_full_run(self, monkeypatch):
        """列裁剪回测与全量列回测的成交记录完全一致。"""
        pruned = _run(_strategy())
        full = _run(_strategy(), monkeypatch=monkeypatch, full_columns=True)
        assert _normalize(pruned["trade_log"]) == _normalize(full["trade_log"])
        assert len(pruned["account_daily"]) == len(full["account_daily"])

    def test_materialize_only_factor_in_columns(self):
        """materialize_only 因子仍被引擎物化到 bars_df 列中。"""
        specs = [
            {"name": "mom20", "weight": 1.0, "ascending": False},
            {"name": "low_turnover", "weight": 0.5, "ascending": True,
             "materialize_only": True},
        ]
        s = _strategy(factor_specs=specs)
        engine = Engine(s, DataProvider(MockDataBackend()),
                       initial_capital=1_000_000, db_path=":memory:")
        engine.run("20240603", "20240628")
        # materialize_only 的 low_turnover 仍然在列中
        assert "low_turnover" in engine.bars_df.columns
        assert "mom20" in engine.bars_df.columns
