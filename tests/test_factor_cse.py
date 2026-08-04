"""btcore.factors.cse 测试：重写正确性 + 物化结果与无 CSE 逐值相等。"""

import numpy as np
import pandas as pd
import pytest

from btcore.factors import cse, ops, plan


def _mk_panel(dates, syms, seed=1):
    idx = pd.MultiIndex.from_product(
        [dates, syms], names=["trade_date", "symbol"]
    )
    rng = np.random.default_rng(seed)
    close = pd.Series(
        rng.uniform(0.9, 1.1, len(idx)).cumsum() / len(dates) + 10, index=idx
    )
    return pd.DataFrame({"close_hfq": close})


class TestRewrite:
    def test_dedup_identical_expr(self):
        nodes = {
            "a": {"expr": "roc(close_hfq, 20)"},
            "b": {"expr": "roc(close_hfq, 20)"},
        }
        out = cse.rewrite(nodes)
        assert out["b"]["expr"] == "a"
        assert out["a"]["expr"] == "roc(close_hfq, 20)"

    def test_dedup_respects_where(self):
        nodes = {
            "a": {"expr": "roc(close_hfq, 20)", "where": "close_hfq > 1"},
            "b": {"expr": "roc(close_hfq, 20)"},
        }
        out = cse.rewrite(nodes)
        assert out["b"]["expr"] != "a"                # where 不同不合并
        assert out["a"].get("where") == "close_hfq > 1"

    def test_no_mutation_of_input(self):
        nodes = {
            "a": {"expr": "roc(close_hfq, 20)"},
            "b": {"expr": "roc(close_hfq, 20)"},
        }
        cse.rewrite(nodes)
        assert nodes["b"]["expr"] == "roc(close_hfq, 20)"

    def test_extract_common_subexpr(self):
        nodes = {
            "f1": {"expr": "ma(close_hfq, 20) * 2"},
            "f2": {"expr": "roc(close_hfq, 5) + ma(close_hfq, 20)"},
        }
        out = cse.rewrite(nodes)
        temps = [n for n in out if n.startswith(cse.TEMP_PREFIX)]
        assert len(temps) == 1
        assert out[temps[0]]["expr"] == "ma(close_hfq, 20)"
        assert temps[0] in out["f1"]["expr"]
        assert temps[0] in out["f2"]["expr"]

    def test_collapse_subtree_not_extracted(self):
        """含坍缩算子的子树不提取（两面板供给语义不同）。"""
        nodes = {
            "f1": {"expr": "mean(close_hfq) + 1"},
            "f2": {"expr": "mean(close_hfq) * 2"},
        }
        out = cse.rewrite(nodes)
        assert not any(n.startswith(cse.TEMP_PREFIX) for n in out)

    def test_single_occurrence_not_extracted(self):
        nodes = {
            "f1": {"expr": "ma(close_hfq, 20) * 2"},
            "f2": {"expr": "ma(close_hfq, 10) * 3"},
        }
        out = cse.rewrite(nodes)
        assert not any(n.startswith(cse.TEMP_PREFIX) for n in out)


class TestMaterializeEquivalence:
    """物化结果必须与无 CSE 的独立求值逐值相等（CSE 是纯重写）。"""

    @pytest.fixture()
    def panel(self):
        dates = pd.date_range("2024-01-01", periods=40).strftime("%Y%m%d")
        return _mk_panel(dates, ["A", "B", "C"])

    def _reference(self, df, nodes):
        for name, spec in nodes.items():
            df[name] = ops.eval_op_expr(df, spec["expr"])
            if spec.get("where"):
                df[name] = df[name].where(df.eval(spec["where"]))
        return df

    def test_subexpr_cse_values_equal(self, panel):
        nodes = {
            "f1": {"expr": "ma(close_hfq, 20) * 2"},
            "f2": {"expr": "roc(close_hfq, 5) + ma(close_hfq, 20)"},
        }
        p = plan.build_factor_plan(nodes, ["f1", "f2"])
        assert p["cse_temp"]                          # 确实发生了提取
        plan.materialize(panel, None, p)
        ref = self._reference(panel.copy(), nodes)
        for col in ("f1", "f2"):
            assert ((panel[col] - ref[col]).abs().fillna(0) < 1e-12).all(), col
            assert (panel[col].isna() == ref[col].isna()).all(), col
        for tmp in p["cse_temp"]:                     # 临时列已删除
            assert tmp not in panel.columns

    def test_dedup_alias_values_equal(self, panel):
        nodes = {
            "a": {"expr": "roc(close_hfq, 20)"},
            "b": {"expr": "roc(close_hfq, 20)"},
        }
        p = plan.build_factor_plan(nodes, ["a", "b"])
        plan.materialize(panel, None, p)
        assert ((panel["a"] - panel["b"]).abs().fillna(0) < 1e-12).all()
        assert (panel["a"].isna() == panel["b"].isna()).all()

    def test_where_untouched(self, panel):
        nodes = {
            "f1": {"expr": "ma(close_hfq, 20)", "where": "close_hfq > 10.5"},
            "f2": {"expr": "ma(close_hfq, 20) * 2"},
        }
        p = plan.build_factor_plan(nodes, ["f1", "f2"])
        plan.materialize(panel, None, p)
        ref = self._reference(panel.copy(), nodes)
        assert (panel["f1"].isna() == ref["f1"].isna()).all()
        assert ((panel["f1"] - ref["f1"]).abs().fillna(0) < 1e-12).all()
        assert ((panel["f2"] - ref["f2"]).abs().fillna(0) < 1e-12).all()
