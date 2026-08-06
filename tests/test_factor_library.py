"""btcore.factors.library 测试：因子库的加载、校验、DAG 与递归计算。"""

import pandas as pd
import pytest

from btcore.factors.library import (
    compute_breadth,
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

    def test_duplicate_key_rejected(self, tmp_path):
        """重复键 fail-fast：PyYAML 默认后者静默覆盖，必须显式报错。"""
        path = _write_lib(
            tmp_path,
            "factors:\n  dup:\n    expr: \"close\"\n  dup:\n    expr: \"open\"\n",
        )
        with pytest.raises(ValueError, match="重复键"):
            load_library(path)


class TestCompute:
    def test_cross_section_where_post_mask(self):
        """where 统一为求值后掩码：被过滤行保留在索引里、值为 NaN。"""
        df = pd.DataFrame(
            {"dv_ttm": [2.0, 3.0, 1.0], "pb": [1.0, 2.0, -1.0]},
            index=["A", "B", "C"],
        )
        values = compute_factors(["value"], df)["value"]
        assert values["A"] == pytest.approx(2.0)
        assert values["B"] == pytest.approx(1.5)
        assert pd.isna(values["C"])

    def test_ts_factor_on_panel(self):
        df = _panel(30)
        values = compute_factors(["mom20"], df)["mom20"]
        d = df.index.get_level_values("trade_date").max()
        close_a = df["close_hfq"].loc[:, "A"]
        assert values[(d, "A")] == pytest.approx(
            close_a.iloc[-1] / close_a.iloc[-21] - 1
        )

    def test_recursive_refs(self, tmp_path):
        """引用未物化的命名因子时递归计算（zscore(mom20) 不依赖 mom20 列）。"""
        lib = load_library()
        df = _panel(30)
        values = compute_factors(["mom_z"], df, lib)["mom_z"]
        d = df.index.get_level_values("trade_date").max()
        mom = compute_factors(["mom20"], df, lib)["mom20"].loc[d]
        assert values[(d, "A")] == pytest.approx(
            (mom["A"] - mom.mean()) / mom.std()
        )

    def test_unknown_name(self):
        df = pd.DataFrame({"close": [1.0]}, index=["A"])
        with pytest.raises(ValueError, match="可用"):
            compute_factors(["nope"], df)

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
        values = compute_factors(["mom3"], bars, lib)["mom3"]
        assert values.index.names == ["trade_date", "symbol"]
        fwd = bars["pct_chg"].groupby(level="symbol").shift(-1)
        ic, _ = calc_ic(values, fwd, date_col="trade_date")
        assert len(ic) > 0


class TestWhereNaNUnified:
    """F-EX-02：where 值为 NaN 时，算子路径与纯表达式路径同为掩码语义。"""

    def _panel(self):
        idx = pd.MultiIndex.from_product(
            [["20240603", "20240604", "20240605", "20240606"], ["A", "B"]],
            names=["trade_date", "symbol"],
        )
        # from_product 展平按行交替：A 序列 [10,12,10,12]，B 序列 [11,13,11,13]
        df = pd.DataFrame(
            {"close": [10.0, 11.0, 12.0, 13.0] * 2,
             "w": [float("nan"), 1.0, 1.0, 1.0] * 2},
            index=idx,
        )
        return df

    def test_op_where_nan_masks(self):
        """算子 where 值 NaN → 掩码（不再被 astype(bool) 转 True 保留）。

        where "delay(close, 2)" 前两日 NaN：旧行为 NaN→True 保留
        （第 2 日 roc 有值也被保留），新行为掩码。
        """
        lib = {"f": {"expr": "roc(close, 1)", "where": "delay(close, 2)"}}
        out = compute_factors(["f"], self._panel(), lib)["f"]
        # 首日 roc 无值 + 前两日 where=NaN 掩码 → (0603/0604) 全 NaN
        assert out.loc[("20240603", "A")] != out.loc[("20240603", "A")]
        assert out.loc[("20240604", "A")] != out.loc[("20240604", "A")]
        # 第 3 日起 where = delay(close,2) 有值 → 保留 roc（A 序列 10→12→10→12）
        assert out.loc[("20240605", "A")] == pytest.approx(10.0 / 12.0 - 1.0)
        assert out.loc[("20240606", "A")] == pytest.approx(12.0 / 10.0 - 1.0)

    def test_plain_where_nan_masks(self):
        """纯表达式 where 值 NaN → 掩码（既有语义，对照锚点）。"""
        lib = {"g": {"expr": "close / 10", "where": "w"}}
        out = compute_factors(["g"], self._panel(), lib)["g"]
        assert out.loc[("20240603", "A")] != out.loc[("20240603", "A")]  # w=NaN
        assert out.loc[("20240603", "B")] == pytest.approx(1.1)  # w=1.0 保留
        assert out.loc[("20240604", "A")] == pytest.approx(1.2)

    def test_op_where_nan_equals_plain_semantics(self):
        """两路径 NaN 语义一致：where 值 NaN 的行都被掩码。"""
        df = self._panel()
        lib_op = {"f": {"expr": "roc(close, 1)", "where": "delay(close, 2)"}}
        lib_plain = {"g": {"expr": "close / 10", "where": "w"}}
        f = compute_factors(["f"], df, lib_op)["f"]
        g = compute_factors(["g"], df, lib_plain)["g"]
        # f 前两日（A/B 两行）全掩码；g 仅 (0603,A)（w=NaN）掩码
        for d in ("20240603", "20240604"):
            for s in ("A", "B"):
                assert f.loc[(d, s)] != f.loc[(d, s)]
        assert g.loc[("20240603", "A")] != g.loc[("20240603", "A")]
        assert g.loc[("20240603", "B")] == pytest.approx(1.1)


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
            compute_breadth("mom", backend, lib, "20240603", "20240607")

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
            "pct_above", backend, lib, "20240603", "20240614", chunk_days=10
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
        result = compute_breadth("pct_above", backend, lib, "20240603", "20240614")
        assert isinstance(result, pd.Series)
        assert len(result) == 0

    def test_warmup_lookback(self):
        """回归：同一日期的值不随请求起点漂移（warmup 缺失曾导致前段静默 0.0）。"""
        lib = load_library()
        backend = MockDataBackend()
        cal = backend.get_calendar("20240603", "20240628")
        # adv_dec_ratio = mean(roc(close_hfq, 1) > 0)，窗口 2 行
        full = compute_breadth("adv_dec_ratio", backend, lib, cal[0], cal[10])
        late = compute_breadth("adv_dec_ratio", backend, lib, cal[3], cal[10])
        assert full.loc[cal[3]] == pytest.approx(late.loc[cal[3]])
        assert full.loc[cal[3]] != 0.0

    def test_log_mktcap_collapse_requests_total_mv(self, tmp_path):
        """归并 plan 口径回归：log_mktcap 坍缩因子自动补请求 total_mv。

        改动前手搓列推导只 expand_columns（伪列直接丢弃），从不补 total_mv，
        ensure_pseudo_columns 取 df["total_mv"] 直接 KeyError。
        """
        path = _write_lib(
            tmp_path,
            "factors:\n  mktcap_mean:\n    expr: \"mean(log_mktcap + close)\"\n",
        )
        lib = load_library(path)
        backend = MockDataBackend()
        result = compute_breadth(
            "mktcap_mean", backend, lib, "20240603", "20240614"
        )
        assert len(result) > 0
        assert result.notna().all()  # fixture total_mv 全正，log 后无 NaN

    def test_idx_ret_collapse_needs_benchmark(self, tmp_path):
        """归并 plan 口径回归：idx_ret 坍缩因子透传 benchmark 后可用。

        改动前 ensure_pseudo_columns 拿不到 benchmark，引用 idx_ret 的坍缩
        因子必抛 ValueError；不传 benchmark 仍须 fail-fast（与引擎同口径）。
        """
        path = _write_lib(
            tmp_path,
            "factors:\n  idx_mean:\n    expr: \"mean(idx_ret + close)\"\n",
        )
        lib = load_library(path)
        backend = MockDataBackend()
        with pytest.raises(ValueError, match="benchmark"):
            compute_breadth("idx_mean", backend, lib, "20240603", "20240614")
        result = compute_breadth(
            "idx_mean", backend, lib, "20240603", "20240614",
            benchmark="000300.SH",
        )
        assert result.dropna().shape[0] > 0  # 首日 pct_change 为 NaN 属正常

    def test_group_mean_values_match_hand_compute(self, tmp_path):
        """F-BRD-03 回归：group_mean 坍缩因子 = 各行业组值等权平均。

        此前 first() 只取排序首行（A 股所在行业组 20.0，任意性）；
        显式口径 = mean(IND1=20, IND2=30) = 25.0。
        """

        class TinyBackend:
            """4 股 × 5 日 × 2 行业的最小合成 backend（收盘价逐日不变）。"""

            def __init__(self):
                dates = ["20240603", "20240604", "20240605",
                         "20240606", "20240607"]
                self._cal = dates
                idx = pd.MultiIndex.from_product(
                    [dates, ["A", "B", "C", "D"]],
                    names=["trade_date", "symbol"],
                )
                # A/B=IND1（均值 20），C/D=IND2（均值 30）
                self._bars = pd.DataFrame(
                    {"close": [10.0, 30.0, 20.0, 40.0] * len(dates)}, index=idx
                )

            def get_calendar(self, start, end):
                return [d for d in self._cal if start <= d <= end]

            def query_bars(self, symbols, start, end, columns=None):
                dates = self._bars.index.get_level_values("trade_date")
                df = self._bars[(dates >= start) & (dates <= end)].copy()
                if columns is not None:
                    df = df.loc[:, sorted(set(columns))]
                return df

            def get_stock_industries(self, ts_codes):
                return {c: ("IND1" if c in ("A", "B") else "IND2")
                        for c in ts_codes}

        path = _write_lib(
            tmp_path,
            "factors:\n  ind_mean:\n    expr: \"group_mean(close, industry)\"\n",
        )
        lib = load_library(path)
        result = compute_breadth(
            "ind_mean", TinyBackend(), lib, "20240603", "20240607"
        )
        assert list(result.index) == TinyBackend().get_calendar("20240603",
                                                                "20240607")
        # 组值 [20.0, 30.0] 等权平均；此前 first() 返回 20.0（任意行业组）
        assert (result == 25.0).all()


class TestBoolSumSemantics:
    """回归：比较结果相加必须为算术计数，而非 bool OR。

    2026-08-03 实证（F-EMA-01）：`(a > b) + (c > d)` 在 numexpr 纯表达式
    路径下 bool 加法=OR，ema_bullish 仅得 0/1 而非 0-3 分，导致策略
    `float(eb) < 3` 恒真、TREND_BREAK 的 EMA 信号永久激活。修复为各比较项
    显式 `* 1` 转数值后再相加。
    """

    def _panel(self) -> pd.DataFrame:
        dates = ["20240102", "20240103"]
        idx = pd.MultiIndex.from_product(
            [dates, ["A", "B", "C"]], names=["trade_date", "symbol"]
        )
        data = {
            # A: 全空头排列(0分)  B: 2/3多头(2分)  C: 全多头(3分)
            "ema_5": [1, 3, 4, 1, 3, 4],
            "ema_20": [2, 2, 3, 2, 2, 3],
            "ema_60": [3, 4, 2, 3, 4, 2],
            "ema_250": [4, 1, 1, 4, 1, 1],
            # 空头信号：macd_dif<=dea / close<bbi / ema和<3 / pdi<mdi
            "macd_dif": [1, 1, 3, 1, 1, 3],
            "macd_dea": [2, 2, 2, 2, 2, 2],
            "close": [5, 5, 3, 5, 5, 3],
            "bbi": [3, 3, 3, 3, 3, 3],
            "dmi_pdi": [6, 6, 6, 6, 6, 6],
            "dmi_mdi": [1, 1, 1, 1, 1, 1],
        }
        return pd.DataFrame(data, index=idx)

    def test_ema_bullish_is_arithmetic_score(self):
        df = self._panel()
        values = compute_factors(["ema_bullish"], df)["ema_bullish"]
        expect = (
            (df["ema_5"] > df["ema_20"]).astype(int)
            + (df["ema_20"] > df["ema_60"]).astype(int)
            + (df["ema_60"] > df["ema_250"]).astype(int)
        )
        pd.testing.assert_series_equal(
            values, expect.astype(float).rename(values.name), check_dtype=False
        )
        assert values.max() == 3.0  # 全多头行必须得 3 分（修复前 OR 只得 1）

    def test_bear_signal_count_is_arithmetic_score(self):
        df = self._panel()
        values = compute_factors(["bear_signal_count"], df)["bear_signal_count"]
        ema_sum = (
            (df["ema_5"] > df["ema_20"]).astype(int)
            + (df["ema_20"] > df["ema_60"]).astype(int)
            + (df["ema_60"] > df["ema_250"]).astype(int)
        )
        expect = (
            (df["macd_dif"] <= df["macd_dea"]).astype(int)
            + (df["close"] < df["bbi"]).astype(int)
            + (ema_sum < 3).astype(int)
            + (df["dmi_pdi"] < df["dmi_mdi"]).astype(int)
        )
        pd.testing.assert_series_equal(
            values, expect.astype(float).rename(values.name), check_dtype=False
        )
        assert values.max() >= 2.0  # 必须出现多信号叠加值（修复前 OR 只得 0/1）


class TestBreadthGroupMean:
    """回归：compute_breadth 对 group_mean 坍缩因子须附着 industry 伪列。

    2026-08-03 实证（F-BRD-02）：此前从不调用 ensure_pseudo_columns，
    industry_mom（group_mean(mom20, industry)）直接 ValueError
    "未知列或因子引用 industry"；docs"引擎同源"对 group_mean 不成立。
    """

    def test_group_mean_attaches_industry(self, tmp_path):
        class IndustryBackend(MockDataBackend):
            def get_stock_industries(self, ts_codes):
                return {c: f"IND_{c[:3]}" for c in ts_codes}

        backend = IndustryBackend()
        # fixture 仅覆盖 20240603-20240701（21 交易日），mom20 全程在 warmup 内；
        # 用短窗口 roc(close_hfq, 1) 验证 industry 伪列附着与分组求值
        path = _write_lib(
            tmp_path,
            "factors:\n"
            "  ind_mom1:\n"
            '    expr: "group_mean(roc(close_hfq, 1), industry)"\n',
        )
        lib = load_library(path)
        result = compute_breadth(
            "ind_mom1", backend, lib, "20240603", "20240628", chunk_days=10
        )
        assert isinstance(result, pd.Series)
        assert len(result) > 0
        assert result.dropna().shape[0] > 0
        # 与全量 compute_factors 一致（伪列附着后"引擎同源"成立）。
        # 注意：fixture bars 自带 close_hfq 原始列与 close×adj_factor 派生有 5e-6
        # 浮点差，两路径必须同用派生列（columns 受限查询 → derive_fields）
        bars = backend.query_bars(None, "20240603", "20240628",
                                  columns=["close", "adj_factor"])
        bars.sort_index(inplace=True)
        factor_plan = __import__("btcore.factors.plan", fromlist=["x"])
        factor_plan.derive_fields(bars)
        needs = {"industry_main": True}
        factor_plan.ensure_pseudo_columns(bars, needs, "main", backend=backend)
        full = compute_factors(["ind_mom1"], bars, lib)
        # F-BRD-03 显式口径：breadth 标量 = 各行业组值等权平均
        # （此前 first() 取排序首行所属行业组，任意性有损）
        grouped = full["ind_mom1"].to_frame().assign(
            industry=bars["industry"].to_numpy()
        )
        per_group = grouped.groupby(
            [grouped.index.get_level_values("trade_date"), "industry"]
        )["ind_mom1"].first()
        group_mean = per_group.groupby(level="trade_date").mean()
        common = group_mean.index.intersection(result.index)
        pd.testing.assert_series_equal(
            group_mean.loc[common].astype(float), result.loc[common].astype(float),
            check_names=False, rtol=1e-6,
        )
