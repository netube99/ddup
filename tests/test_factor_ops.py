"""btcore.factors.ops 测试：算子正确性、表达式结构分析、求值器。"""

import numpy as np
import pandas as pd
import pytest

from btcore.factors import ops


def _panel(rows: int = 30, syms=("A", "B", "C", "D", "E", "F")) -> pd.DataFrame:
    dates = pd.date_range("2024-01-01", periods=rows).strftime("%Y%m%d")
    idx = pd.MultiIndex.from_product(
        [dates, list(syms)], names=["trade_date", "symbol"]
    )
    rng = np.random.default_rng(0)
    close = pd.Series(
        rng.uniform(0.9, 1.1, len(idx)).cumsum() / rows + 10, index=idx
    )
    df = pd.DataFrame({"close_hfq": close, "up_limit": close * 1.1})
    df["pct_chg"] = close.groupby(level="symbol").pct_change()
    ind = {s: ("I1" if i % 2 == 0 else "I2") for i, s in enumerate(syms)}
    df["industry"] = df.index.get_level_values("symbol").map(ind)
    return df


@pytest.fixture()
def panel() -> pd.DataFrame:
    return _panel()


@pytest.fixture()
def last_date(panel) -> str:
    return panel.index.get_level_values("trade_date").max()


class TestTsOps:
    def test_ma(self, panel, last_date):
        v = ops.eval_op_expr(panel, "ma(close_hfq, 5)")
        ref = panel["close_hfq"].loc[:, "A"].iloc[-5:].mean()
        assert v[(last_date, "A")] == pytest.approx(ref)

    def test_roc(self, panel, last_date):
        v = ops.eval_op_expr(panel, "roc(close_hfq, 5)")
        close = panel["close_hfq"].loc[:, "A"]
        assert v[(last_date, "A")] == pytest.approx(close.iloc[-1] / close.iloc[-6] - 1)

    def test_delay_delta(self, panel, last_date):
        close = panel["close_hfq"].loc[:, "A"]
        d = ops.eval_op_expr(panel, "delay(close_hfq, 3)")
        assert d[(last_date, "A")] == close.iloc[-4]
        delta = ops.eval_op_expr(panel, "delta(close_hfq, 3)")
        assert delta[(last_date, "A")] == pytest.approx(close.iloc[-1] - close.iloc[-4])

    def test_ema(self, panel, last_date):
        v = ops.eval_op_expr(panel, "ema(close_hfq, 5)")
        ref = panel["close_hfq"].loc[:, "A"].ewm(span=5, adjust=False).mean()
        assert v[(last_date, "A")] == pytest.approx(ref.iloc[-1])

    def test_std_sum_max_min(self, panel, last_date):
        close = panel["close_hfq"].loc[:, "A"]
        assert ops.eval_op_expr(panel, "std(close_hfq, 10)")[(last_date, "A")] == \
            pytest.approx(close.iloc[-10:].std())
        assert ops.eval_op_expr(panel, "sum(close_hfq, 10)")[(last_date, "A")] == \
            pytest.approx(close.iloc[-10:].sum())
        assert ops.eval_op_expr(panel, "max(close_hfq, 10)")[(last_date, "A")] == \
            close.iloc[-10:].max()
        assert ops.eval_op_expr(panel, "min(close_hfq, 10)")[(last_date, "A")] == \
            close.iloc[-10:].min()

    def _moment_refs(self, panel, n=10):
        x = panel["pct_chg"].loc[:, "A"].iloc[-n:].to_numpy()
        y = panel["close_hfq"].loc[:, "A"].iloc[-n:].to_numpy()
        return x, y

    def test_beta_matches_numpy(self, panel, last_date):
        v = ops.eval_op_expr(panel, "beta(pct_chg, close_hfq, 10)")
        x, y = self._moment_refs(panel)
        ref = np.cov(x, y, ddof=0)[0, 1] / np.var(y)
        assert v[(last_date, "A")] == pytest.approx(ref)

    def test_corr_matches_numpy(self, panel, last_date):
        v = ops.eval_op_expr(panel, "corr(pct_chg, close_hfq, 10)")
        x, y = self._moment_refs(panel)
        assert v[(last_date, "A")] == pytest.approx(np.corrcoef(x, y)[0, 1])

    def test_resid_std_matches_numpy(self, panel, last_date):
        v = ops.eval_op_expr(panel, "resid_std(pct_chg, close_hfq, 10)")
        x, y = self._moment_refs(panel)
        xmat = np.column_stack([np.ones(len(y)), y])
        coef, *_ = np.linalg.lstsq(xmat, x, rcond=None)
        resid = x - xmat @ coef
        assert v[(last_date, "A")] == pytest.approx(resid.std())


class TestXsecOps:
    def test_rank(self, panel, last_date):
        v = ops.eval_op_expr(panel, "rank(close_hfq)")
        assert sorted(v.loc[last_date].tolist()) == \
            pytest.approx([i / 6 for i in range(1, 7)])

    def test_zscore(self, panel, last_date):
        v = ops.eval_op_expr(panel, "zscore(close_hfq)")
        assert v.loc[last_date].mean() == pytest.approx(0.0, abs=1e-9)

    def test_winsorize(self, panel, last_date):
        v = ops.eval_op_expr(panel, "winsorize(close_hfq, 0.2)")
        close = panel["close_hfq"].loc[last_date]
        assert v.loc[last_date].max() <= close.quantile(0.8) + 1e-9
        assert v.loc[last_date].min() >= close.quantile(0.2) - 1e-9

    def test_group_rank(self, panel, last_date):
        v = ops.eval_op_expr(panel, "group_rank(close_hfq, industry)")
        # 每组 3 只：组内 rank ∈ {1/3, 2/3, 1}
        assert sorted(v.loc[last_date].iloc[[0, 2, 4]].tolist()) == \
            pytest.approx([1 / 3, 2 / 3, 1.0])

    def test_neutralize_matches_numpy(self, panel, last_date):
        v = ops.eval_op_expr(panel, "neutralize(pct_chg, industry, close_hfq)")
        sub = panel.loc[last_date]
        xmat = np.column_stack([
            np.ones(6),
            [0, 1, 0, 1, 0, 1],  # 行业哑变量（drop_first）
            sub["close_hfq"].to_numpy(),
        ])
        ok = sub["pct_chg"].notna().to_numpy()
        coef, *_ = np.linalg.lstsq(xmat[ok], sub["pct_chg"].to_numpy()[ok], rcond=None)
        ref = sub["pct_chg"].to_numpy()[ok] - xmat[ok] @ coef
        assert np.allclose(v.loc[last_date].to_numpy()[ok], ref)

    def test_mean_broadcast(self, panel, last_date):
        v = ops.eval_op_expr(panel, "mean(close_hfq)")
        assert v.loc[last_date].nunique() == 1
        assert v.loc[last_date].iloc[0] == \
            pytest.approx(panel["close_hfq"].loc[last_date].mean())

    def test_group_mean(self, panel, last_date):
        v = ops.eval_op_expr(panel, "group_mean(close_hfq, industry)")
        close = panel["close_hfq"].loc[last_date]
        assert v.loc[(last_date, "A")] == \
            pytest.approx(close.iloc[[0, 2, 4]].mean())


class TestComposite:
    def test_mixed_axes_nesting(self, panel, last_date):
        """ts 算子嵌在坍缩 xsec 算子里：站上 MA20 比例。"""
        v = ops.eval_op_expr(panel, "mean(close_hfq >= ma(close_hfq, 20))")
        ma20 = panel["close_hfq"].groupby(level="symbol").rolling(20).mean()
        above = (panel["close_hfq"] >= ma20.droplevel(0).reindex(panel.index))
        assert v.loc[last_date].iloc[0] == pytest.approx(above.loc[last_date].mean())

    def test_compare_produces_zero_one(self, panel):
        v = ops.eval_op_expr(panel, "close_hfq >= up_limit")
        assert set(v.dropna().unique()) <= {0.0, 1.0}

    def test_event_count(self, panel, last_date):
        """事件计数 = ts sum 作用于布尔表达式。"""
        v = ops.eval_op_expr(panel, "sum(close_hfq >= up_limit * 0.999, 10)")
        assert v.loc[last_date].max() == 0  # up_limit = close*1.1, 不会触板

    def test_boolop(self, panel):
        v = ops.eval_op_expr(panel, "(close_hfq > 10) and (pct_chg > 0)")
        assert set(v.dropna().unique()) <= {0.0, 1.0}


class TestStaticAnalysis:
    def test_has_op_call(self):
        assert ops.has_op_call("roc(close_hfq, 20)")
        assert not ops.has_op_call("dv_ttm / pb")

    def test_extract_names(self):
        cols, refs = ops.extract_op_names(
            "mean(close_hfq >= ma(close_hfq, 20))", set()
        )
        assert cols == {"close_hfq"} and refs == set()
        cols, refs = ops.extract_op_names(
            "mom20 - industry_mom", {"mom20", "industry_mom"}
        )
        assert cols == set() and refs == {"mom20", "industry_mom"}

    def test_infer_window(self):
        assert ops.infer_window("roc(close_hfq, 20)", {}) == 21
        assert ops.infer_window("ma(roc(close_hfq, 20), 10)", {}) == 30
        assert ops.infer_window("beta(pct_chg, idx_ret, 250)", {}) == 250
        assert ops.infer_window("zscore(mom20)", {"mom20": 21}) == 21

    def test_collapse_kind(self):
        assert ops.collapse_kind("mean(close_hfq >= ma(close_hfq, 20))") == "market"
        assert ops.collapse_kind("group_mean(mom20, industry)") == "group"
        assert ops.collapse_kind("zscore(mom20)") is None

    def test_validate_rejects(self):
        with pytest.raises(ValueError, match="未知算子"):
            ops.validate_op_expr("magic(close, 1)")
        with pytest.raises(ValueError, match="禁止"):
            ops.validate_op_expr("close.apply(1)")
        with pytest.raises(ValueError, match="正整数"):
            ops.validate_op_expr("ma(close, 0)")
        with pytest.raises(ValueError, match="参数"):
            ops.validate_op_expr("ma(close)")
        with pytest.raises(ValueError, match="链式比较"):
            ops.validate_op_expr("1 < close < 2")
