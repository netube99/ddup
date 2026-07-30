"""btcore.factors.library 测试：因子库的加载、校验、DAG 与递归计算。"""

import pandas as pd
import pytest

from btcore.factors.library import (
    compute_breadth,
    compute_factor,
    compute_factors,
    load_library,
    resolve_closure,
    resolve_spec,
)
from research.factor_eval import calc_ic
from tests.conftest import MockDataBackend


def _write_lib(tmp_path, body: str) -> str:
    path = tmp_path / "lib.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _panel(rows: int = 30) -> pd.DataFrame:
    """3 只股票 × rows 天的合成面板。"""
    dates = pd.date_range("2024-01-01", periods=rows).strftime("%Y%m%d")
    idx = pd.MultiIndex.from_product(
        [dates, ["A", "B", "C"]], names=["trade_date", "symbol"]
    )
    close = pd.Series(
        [10 + i * 0.1 + j for i in range(rows) for j in range(3)], index=idx
    )
    return pd.DataFrame({"close_hfq": close})


class TestLoad:
    def test_load_default_library(self):
        lib = load_library()
        assert lib["mom20"]["expr"] == "roc(close_hfq, 20)"
        assert lib["value"]["where"] == "pb > 0"

    def test_missing_factors_key(self, tmp_path):
        path = _write_lib(tmp_path, "something_else: {}\n")
        with pytest.raises(ValueError, match="factors"):
            load_library(path)

    def test_missing_expr(self, tmp_path):
        path = _write_lib(tmp_path, "factors:\n  bad:\n    description: x\n")
        with pytest.raises(ValueError, match="缺少 expr"):
            load_library(path)

    def test_invalid_plain_expr(self, tmp_path):
        path = _write_lib(tmp_path, 'factors:\n  bad:\n    expr: "close +"\n')
        with pytest.raises(ValueError, match="表达式非法"):
            load_library(path)

    def test_plain_expr_rejects_call(self, tmp_path):
        path = _write_lib(tmp_path, 'factors:\n  bad:\n    expr: "close.apply(1)"\n')
        with pytest.raises(ValueError, match="表达式非法"):
            load_library(path)

    def test_unknown_operator_rejected(self, tmp_path):
        path = _write_lib(tmp_path, 'factors:\n  bad:\n    expr: "magic(close, 3)"\n')
        with pytest.raises(ValueError, match="未知算子"):
            load_library(path)

    def test_reserved_name_rejected(self, tmp_path):
        path = _write_lib(tmp_path, 'factors:\n  close:\n    expr: "open"\n')
        with pytest.raises(ValueError, match="保留列名"):
            load_library(path)

    def test_cycle_rejected(self, tmp_path):
        path = _write_lib(
            tmp_path,
            "factors:\n  a:\n    expr: \"b + 1\"\n  b:\n    expr: \"a + 1\"\n",
        )
        with pytest.raises(ValueError, match="环"):
            load_library(path)


class TestCompute:
    def test_cross_section_where_post_mask(self):
        """where 统一为求值后掩码：被过滤行保留在索引里、值为 NaN。"""
        df = pd.DataFrame(
            {"dv_ttm": [2.0, 3.0, 1.0], "pb": [1.0, 2.0, -1.0]},
            index=["A", "B", "C"],
        )
        values = compute_factor("value", df)
        assert values["A"] == pytest.approx(2.0)
        assert values["B"] == pytest.approx(1.5)
        assert pd.isna(values["C"])

    def test_ts_factor_on_panel(self):
        df = _panel(30)
        values = compute_factor("mom20", df)
        d = df.index.get_level_values("trade_date").max()
        close_a = df["close_hfq"].loc[:, "A"]
        assert values[(d, "A")] == pytest.approx(
            close_a.iloc[-1] / close_a.iloc[-21] - 1
        )

    def test_recursive_refs(self, tmp_path):
        """引用未物化的命名因子时递归计算（zscore(mom20) 不依赖 mom20 列）。"""
        lib = load_library()
        df = _panel(30)
        values = compute_factor("mom_z", df, lib)
        d = df.index.get_level_values("trade_date").max()
        mom = compute_factor("mom20", df, lib).loc[d]
        assert values[(d, "A")] == pytest.approx(
            (mom["A"] - mom.mean()) / mom.std()
        )

    def test_unknown_name(self):
        df = pd.DataFrame({"close": [1.0]}, index=["A"])
        with pytest.raises(ValueError, match="可用"):
            compute_factor("nope", df)

    def test_compute_factors_batch(self):
        df = _panel(30)
        df["turnover_rate"] = 5.0
        out = compute_factors(["mom20", "low_turnover"], df)
        assert list(out.columns) == ["mom20", "low_turnover"]

    def test_full_history_feeds_research(self):
        """全历史 MultiIndex 输入 → (date,symbol) Series，直接衔接 calc_ic。"""
        backend = MockDataBackend()
        bars = backend.query_bars(None, "20240603", "20240614")
        # fixture 区间短，用短窗口 ts 因子验证接口衔接
        lib = {"mom3": {"expr": "roc(close_hfq, 3)"}}
        values = compute_factor("mom3", bars, lib)
        assert values.index.names == ["trade_date", "symbol"]
        fwd = bars["pct_chg"].groupby(level="symbol").shift(-1)
        ic, _ = calc_ic(values, fwd, date_col="trade_date")
        assert len(ic) > 0


class TestResolve:
    def test_resolve_spec(self):
        spec = resolve_spec({"factor": "mom20", "weight": 2.0, "ascending": True})
        assert spec == {
            "name": "mom20", "weight": 2.0, "ascending": True, "materialize_only": False
        }

    def test_resolve_spec_rejects_inline_expr(self):
        with pytest.raises(ValueError, match="library.yaml"):
            resolve_spec({"name": "x", "expr": "close"})

    def test_resolve_spec_unknown_factor(self):
        with pytest.raises(ValueError, match="未知因子"):
            resolve_spec({"factor": "nope"})

    def test_resolve_closure_transitive(self):
        closure = resolve_closure(["mom_z"])
        assert set(closure) == {"mom_z", "mom20"}
        assert closure["mom20"]["expr"] == "roc(close_hfq, 20)"

    def test_resolve_closure_unknown(self):
        with pytest.raises(ValueError, match="未知因子"):
            resolve_closure(["nope"])


class TestComputeBreadth:
    """2.5: compute_breadth 流式计算坍缩因子。"""

    def test_rejects_conformal_factor(self, tmp_path):
        """保形因子应抛出 ValueError。"""
        path = _write_lib(tmp_path, "factors:\n  mom:\n    expr: \"roc(close, 3)\"\n")
        lib = load_library(path)
        backend = MockDataBackend()
        with pytest.raises(ValueError, match="仅支持坍缩因子"):
            compute_breadth("mom", backend, "20240603", "20240607", lib)

    def test_collapse_matches_full_compute(self, tmp_path):
        """坍缩因子分块计算应与全量 compute_factors 一致。"""
        path = _write_lib(
            tmp_path,
            "factors:\n  pct_above:\n    expr: \"mean(close >= ma(close, 3))\"\n",
        )
        lib = load_library(path)
        backend = MockDataBackend()

        # 全量计算
        bars_full = backend.query_bars(None, "20240603", "20240614")
        full_result = compute_factors(["pct_above"], bars_full, lib)
        daily_from_full = (
            full_result.groupby(level="trade_date")["pct_above"].first()
        )

        # 流式分块计算（chunk_days=5 覆盖全部日期避免边界效应）
        daily_stream = compute_breadth(
            "pct_above", backend, "20240603", "20240614", lib, chunk_days=10
        )

        # 对齐日期比较
        common = daily_from_full.index.intersection(daily_stream.index)
        assert len(common) > 0, "无交叠日期"
        pd.testing.assert_series_equal(
            daily_from_full.loc[common].astype(float),
            daily_stream.loc[common].astype(float),
            check_names=False,
            rtol=1e-9,
        )

    def test_empty_calendar_returns_empty_series(self, tmp_path):
        """无交易日时应返回空 Series。"""
        path = _write_lib(
            tmp_path,
            "factors:\n  pct_above:\n    expr: \"mean(close >= ma(close, 3))\"\n",
        )
        lib = load_library(path)

        # 创建一个始终返回空日历的 mock backend
        class EmptyCalBackend(MockDataBackend):
            def get_calendar(self, start, end):
                return []

        backend = EmptyCalBackend()
        result = compute_breadth("pct_above", backend, "20240603", "20240614", lib)
        assert isinstance(result, pd.Series)
        assert len(result) == 0
