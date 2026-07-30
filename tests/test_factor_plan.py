"""btcore.factors.plan 测试：供给计划推导 + 两阶段物化 + 因果性 + 引擎 e2e。"""

import logging

import numpy as np
import pandas as pd
import pytest

from btcore.engine import Engine
from btcore.factors import plan
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy
from tests.conftest import MockDataBackend

_NODES = {
    "mom20": {"expr": "roc(close_hfq, 20)"},
    "mom_z": {"expr": "zscore(mom20)"},
    "industry_mom": {"expr": "group_mean(mom20, industry)"},
    "pct_above_ma20": {"expr": "mean(close_hfq >= ma(close_hfq, 20))"},
    "rel_mom": {"expr": "mom20 - industry_mom", "where": "pct_above_ma20 > 0.5"},
}


class TestBuildPlan:
    def test_scopes(self):
        """坍缩节点进广度集合；被两侧引用的节点两个面板都算；坍缩只来自投影。"""
        p = plan.build_factor_plan(_NODES, ["rel_mom", "mom_z"])
        assert p["collapse"] == {"industry_mom": "group", "pct_above_ma20": "market"}
        assert p["breadth"] == {"industry_mom", "pct_above_ma20", "mom20"}
        assert p["main"] == {"mom20", "mom_z", "rel_mom"}
        assert p["main_columns"] == {"close_hfq"}
        assert p["breadth_columns"] == {"close_hfq"}

    def test_needs_flags(self):
        p = plan.build_factor_plan(_NODES, ["rel_mom"])
        assert p["needs"]["market"] is True
        assert p["needs"]["industry_main"] is True   # group 投影需要主面板 industry
        assert p["needs"]["industry_breadth"] is True
        assert p["needs"]["index"] is False

    def test_no_collapse_no_breadth(self):
        p = plan.build_factor_plan(_NODES, ["mom_z"])
        assert p["needs"]["market"] is False
        assert p["breadth"] == set()
        assert p["main"] == {"mom_z", "mom20"}

    def test_index_and_mktcap_flags(self):
        nodes = {
            "b": {"expr": "beta(pct_chg, idx_ret, 250)"},
            "n": {"expr": "neutralize(mom, industry, log_mktcap)"},
            "mom": {"expr": "roc(close_hfq, 20)"},
        }
        p = plan.build_factor_plan(nodes, ["b", "n"])
        assert p["needs"]["index"] is True
        assert p["needs"]["mktcap_main"] is True
        assert "total_mv" in p["main_columns"]

    def test_warmup_days(self):
        p = plan.build_factor_plan(_NODES, ["mom_z"])
        assert p["main_days"] == 365          # 20 行窗口远低于 365 地板
        nodes = {"b": {"expr": "beta(pct_chg, idx_ret, 250)"}}
        p = plan.build_factor_plan(nodes, ["b"])
        assert p["main_days"] > 365           # 长窗口撑大预热
        assert p["breadth_days"] == int(250 * 1.5) + 10


class TestWindows:
    def test_nested_expr(self):
        nodes = {"f": {"expr": "ma(roc(close_hfq, 20), 10)"}}
        p = plan.build_factor_plan(nodes, ["f"])
        assert p["windows"]["f"] == 30        # (1+20) + (10-1)

    def test_ref_transitive(self):
        """引用传递：zscore(mom20) 继承 mom20 的窗口；xsec 不消耗时间轴。"""
        p = plan.build_factor_plan(_NODES, ["rel_mom", "mom_z"])
        w = p["windows"]
        assert w["mom20"] == 21               # 1 + 20
        assert w["mom_z"] == 21
        assert w["industry_mom"] == 21
        assert w["pct_above_ma20"] == 20      # 1 + (20-1)
        assert w["rel_mom"] == 21             # max(21, 21, where 20)

    def test_ema_approximation(self):
        nodes = {"e": {"expr": "ema(close_hfq, 10)"}}
        p = plan.build_factor_plan(nodes, ["e"])
        assert p["windows"]["e"] == 30        # 1 + (3*10-1)，无限记忆的工程近似


def _mk_panel(dates, syms, seed=1):
    idx = pd.MultiIndex.from_product(
        [dates, syms], names=["trade_date", "symbol"]
    )
    rng = np.random.default_rng(seed)
    close = pd.Series(
        rng.uniform(0.9, 1.1, len(idx)).cumsum() / len(dates) + 10, index=idx
    )
    df = pd.DataFrame({"close_hfq": close})
    ind = {s: ("I1" if i % 2 == 0 else "I2") for i, s in enumerate(syms)}
    df["industry"] = df.index.get_level_values("symbol").map(ind)
    return df


class TestMaterialize:
    @pytest.fixture()
    def panels(self):
        dates = pd.date_range("2024-01-01", periods=40).strftime("%Y%m%d")
        return dates, _mk_panel(dates, ["A", "B", "C"]), \
            _mk_panel(dates, ["A", "B", "C", "D", "E"], seed=2)

    def _materialize(self, main, breadth):
        p = plan.build_factor_plan(_NODES, ["rel_mom", "mom_z"])
        plan.materialize(main, breadth, p, _NODES)
        return p

    def test_market_breadth_is_full_market(self, panels):
        """坍缩口径 = 全市场（广度面板含主面板没有的 D/E）。"""
        dates, main, breadth = panels
        self._materialize(main, breadth)
        d = dates[-1]
        ma20 = breadth["close_hfq"].groupby(level="symbol").rolling(20).mean()
        above = (breadth["close_hfq"]
                 >= ma20.droplevel(0).reindex(breadth.index)).astype(float)
        assert main.loc[(d, "A"), "pct_above_ma20"] == \
            pytest.approx(above.loc[d].mean())
        # 广播：同日所有个股同值
        assert main.loc[d, "pct_above_ma20"].nunique() == 1

    def test_group_projection(self, panels):
        """行业聚合 map 回个股：同行业同值，且按全市场同行业成员聚合。"""
        dates, main, breadth = panels
        self._materialize(main, breadth)
        d = dates[-1]
        roc20 = (breadth["close_hfq"]
                 / breadth["close_hfq"].groupby(level="symbol").shift(20) - 1)
        assert main.loc[(d, "A"), "industry_mom"] == \
            pytest.approx(roc20.loc[d][["A", "C", "E"]].mean())   # I1 全市场
        assert main.loc[(d, "B"), "industry_mom"] == \
            pytest.approx(roc20.loc[d][["B", "D"]].mean())        # I2 全市场

    def test_where_post_mask(self, panels):
        _, main, breadth = panels
        self._materialize(main, breadth)
        pct = main["pct_above_ma20"]
        assert main.loc[pct <= 0.5, "rel_mom"].isna().all()
        assert (pct[main["rel_mom"].notna()] > 0.5).all()

    def test_causality(self, panels):
        """篡改未来数据不影响历史物化值（物化列因果，无前视）。"""
        dates, main, breadth = panels
        m1, b1 = main.copy(), breadth.copy()
        self._materialize(m1, b1)
        m2, b2 = main.copy(), breadth.copy()
        cut = dates[-6]
        for df in (m2, b2):
            mask = df.index.get_level_values("trade_date") > cut
            df.loc[mask, "close_hfq"] *= 100
        self._materialize(m2, b2)
        early = m1.index.get_level_values("trade_date") <= cut
        for col in ["mom20", "industry_mom", "pct_above_ma20", "mom_z", "rel_mom"]:
            a, b = m1.loc[early, col], m2.loc[early, col]
            assert (a.isna() == b.isna()).all()
            assert ((a - b).abs().fillna(0) < 1e-9).all(), col
        assert (m1["mom20"][~early] != m2["mom20"][~early]).any()  # 篡改生效


class _IndustryBackend(MockDataBackend):
    """带确定性行业映射的 fixture backend（全市场广度 e2e 用）。"""

    def get_stock_industries(self, ts_codes: list[str]) -> dict[str, str]:
        return {s: f"IND-{hash(s) % 4}" for s in ts_codes}


class TestEngineE2E:
    def _run(self, tmp_path, factor: str) -> Engine:
        yaml_path = tmp_path / "s.yaml"
        yaml_path.write_text(f"""\
strategy: strategies.examples.rolling_ranker:RollingRanker
config:
  top_k: 3
  max_positions: 3
factor_specs:
  - factor: {factor}
""", encoding="utf-8")
        strategy = load_strategy(str(yaml_path))
        engine = Engine(strategy, DataProvider(_IndustryBackend()),
                        initial_capital=1_000_000, db_path=":memory:")
        engine.run("20240603", "20240628")
        return engine

    def test_market_breadth_materialized(self, tmp_path):
        """pct_above_ma20 物化为广播列：同日同值、∈[0,1]、全市场口径。"""
        engine = self._run(tmp_path, "pct_above_ma20")
        col = engine.bars_df["pct_above_ma20"]
        last = engine.bars_df.index.get_level_values("trade_date").max()
        day = engine.bars_df.loc[last, "pct_above_ma20"]
        assert day.nunique() == 1
        assert 0.0 <= day.iloc[0] <= 1.0
        assert col.notna().all()  # 0/1 均值，无 NaN

    def test_industry_breadth_materialized(self, tmp_path):
        """industry_mom map 回个股：同行业同值。"""
        engine = self._run(tmp_path, "industry_mom")
        df = engine.bars_df.dropna(subset=["industry_mom"])
        same = df.groupby(
            [df.index.get_level_values("trade_date"), "industry"]
        )["industry_mom"].nunique()
        assert (same == 1).all()

    def test_idx_ret_beta(self, tmp_path):
        """引用 idx_ret 的参照系因子：benchmark 派生列进面板，末端有值。"""
        (tmp_path / "lib.yaml").write_text(
            'factors:\n  idx_beta:\n    expr: "beta(pct_chg, idx_ret, 5)"\n',
            encoding="utf-8",
        )
        yaml_path = tmp_path / "s.yaml"
        yaml_path.write_text("""\
strategy: strategies.examples.rolling_ranker:RollingRanker
factor_library: lib.yaml
config:
  top_k: 3
  max_positions: 3
factor_specs:
  - factor: idx_beta
""", encoding="utf-8")
        strategy = load_strategy(str(yaml_path))
        engine = Engine(strategy, DataProvider(_IndustryBackend()),
                        initial_capital=1_000_000, db_path=":memory:")
        engine.run("20240603", "20240628")
        assert "idx_ret" in engine.bars_df.columns
        last = engine.bars_df.index.get_level_values("trade_date").max()
        day = engine.bars_df.loc[last, "idx_beta"]
        assert day.notna().any()


class TestCollapseIntegrity:
    """2.1 / 2.2: NaN 告警与物化后验证。"""

    def test_project_logs_missing_dates(self, caplog):
        """广度面板日期少于主面板时 _project 应告警。"""
        import logging

        syms = ["A", "B", "C"]
        main_dates = pd.date_range("2024-01-01", periods=10).strftime("%Y%m%d")
        breadth_dates = pd.date_range("2024-01-05", periods=5).strftime("%Y%m%d")

        main_idx = pd.MultiIndex.from_product(
            [main_dates, syms], names=["trade_date", "symbol"]
        )
        breadth_idx = pd.MultiIndex.from_product(
            [breadth_dates, syms], names=["trade_date", "symbol"]
        )
        main_df = pd.DataFrame({"close_hfq": 1.0}, index=main_idx)
        breadth_df = pd.DataFrame({"close_hfq": 2.0}, index=breadth_idx)
        breadth_df["test_collapse"] = 42.0

        p = plan.build_factor_plan(
            {"test_collapse": {"expr": "mean(close_hfq)"}}, ["test_collapse"]
        )

        with caplog.at_level(logging.WARNING, logger="btcore.factors.plan"):
            plan.materialize(main_df, breadth_df, p, {"test_collapse": {"expr": "mean(close_hfq)"}})

        warnings = [r.message for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("坍缩因子" in m and "无值" in m for m in warnings), \
            f"Expected NaN warning, got: {warnings}"

    def test_validate_materialization_no_issues(self):
        """正常物化无 NaN 时 validate_materialization 返回空列表。"""
        syms = ["A", "B", "C"]
        dates = pd.date_range("2024-01-01", periods=10).strftime("%Y%m%d")
        idx = pd.MultiIndex.from_product(
            [dates, syms], names=["trade_date", "symbol"]
        )

        main_df = pd.DataFrame({"close_hfq": 1.0}, index=idx)
        breadth_df = pd.DataFrame({"close_hfq": 2.0}, index=idx)
        breadth_df["test_collapse"] = 42.0

        p = plan.build_factor_plan(
            {"test_collapse": {"expr": "mean(close_hfq)"}}, ["test_collapse"]
        )
        plan.materialize(main_df, breadth_df, p,
                         {"test_collapse": {"expr": "mean(close_hfq)"}})

        issues = plan.validate_materialization(main_df, p)
        assert issues == [], f"Expected no issues, got: {issues}"

    def test_validate_materialization_detects_nan(self, caplog):
        """NaN 占比超 5% 时 validate_materialization 返回 warning issue。"""
        import logging

        syms = ["A", "B", "C"]
        dates = pd.date_range("2024-01-01", periods=10).strftime("%Y%m%d")

        # 广度面板只有部分日期有值（末尾日期），投影后主面板早日期为 NaN
        main_idx = pd.MultiIndex.from_product(
            [dates, syms], names=["trade_date", "symbol"]
        )
        breadth_dates = dates[-3:]  # 只有最后 3 天有数据
        breadth_idx = pd.MultiIndex.from_product(
            [breadth_dates, syms], names=["trade_date", "symbol"]
        )

        main_df = pd.DataFrame({"close_hfq": 1.0}, index=main_idx)
        breadth_df = pd.DataFrame({"close_hfq": 2.0}, index=breadth_idx)
        breadth_df["test_collapse"] = 42.0

        p = plan.build_factor_plan(
            {"test_collapse": {"expr": "mean(close_hfq)"}}, ["test_collapse"]
        )
        plan.materialize(main_df, breadth_df, p,
                         {"test_collapse": {"expr": "mean(close_hfq)"}})

        # NaN 占比 = 7/10 = 70% > 5%
        issues = plan.validate_materialization(main_df, p)
        assert len(issues) > 0, "Expected issues for high NaN ratio"
        assert any("NaN" in i["message"] for i in issues)
        assert all(i["level"] == "warning" for i in issues)


class TestValidateMaterialization:
    """validate_materialization() 的单元测试。"""

    def test_normal_returns_empty(self):
        """正常物化不产生 issues（足够长的时间序列使 NaN 占比低于 5%）。"""
        dates = pd.date_range("2024-01-01", periods=500).strftime("%Y%m%d")
        main = _mk_panel(dates, ["A", "B", "C"])
        breadth = _mk_panel(dates, ["A", "B", "C", "D", "E"], seed=2)
        p = plan.build_factor_plan(_NODES, ["rel_mom", "mom_z"])
        plan.materialize(main, breadth, p, _NODES)
        issues = plan.validate_materialization(main, p)
        assert len(issues) == 0

    def test_nan_detected(self):
        """含高 NaN 占比的坍缩因子被检出。"""
        dates = pd.date_range("2024-01-01", periods=40).strftime("%Y%m%d")
        main = _mk_panel(dates, ["A", "B", "C"])
        breadth = _mk_panel(dates, ["A", "B", "C", "D", "E"], seed=2)
        p = plan.build_factor_plan(_NODES, ["rel_mom", "mom_z"])
        plan.materialize(main, breadth, p, _NODES)
        # 人为制造大量 NaN
        main.loc[main.index[:200], "pct_above_ma20"] = np.nan
        issues = plan.validate_materialization(main, p)
        assert len(issues) > 0
        assert any("NaN" in issue["message"] for issue in issues)


class TestProjectWarnings:
    """_project() NaN 告警测试。"""

    def test_missing_dates_logs_warning(self, caplog):
        """广度面板日期少于主面板日期时 _project() 输出告警。"""
        with caplog.at_level(logging.WARNING, logger="btcore.factors.plan"):
            dates_main = pd.date_range("2024-01-01", periods=40).strftime("%Y%m%d")
            dates_breadth = pd.date_range("2024-01-01", periods=25).strftime("%Y%m%d")
            main = _mk_panel(dates_main, ["A", "B", "C"])
            breadth = _mk_panel(dates_breadth, ["A", "B", "C", "D", "E"], seed=2)
            p = plan.build_factor_plan(_NODES, ["rel_mom", "mom_z"])
            # 先在广度面板求值
            breadth_set = p["breadth"]
            for name in p["topo"]:
                if name in breadth_set:
                    breadth[name] = plan._eval_spec_on(breadth, _NODES[name])
            # 投影：market 坍缩因子，广度面板日期少 → NaN
            for name, kind in p["collapse"].items():
                plan._project(main, breadth, name, kind)

        has_warning = any("坍缩因子" in r.message for r in caplog.records)
        assert has_warning, "日期不匹配应产生告警"
