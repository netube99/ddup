"""TDD 审查测试：因子子系统（plan/cse/ops/library）正确性。

仅覆盖本轮审查发现的缺陷；既有行为回归由 tests/test_factor_*.py 承担。
"""

import pandas as pd
import pytest

from btcore.factors import plan


def _panels(dates, main_syms, breadth_syms, flag_by_sym, close=10.0):
    """构造主/广度面板：close_hfq 常值 + flag 掩码列（按 symbol）。"""

    def _mk(syms):
        idx = pd.MultiIndex.from_product([dates, syms],
                                         names=["trade_date", "symbol"])
        df = pd.DataFrame({"close_hfq": close}, index=idx)
        df["flag"] = df.index.get_level_values("symbol").map(flag_by_sym)
        return df

    return _mk(main_syms), _mk(breadth_syms)


class TestProjectPreservesWhereMask:
    """docs/factor_library.md §8：where 是后置掩码，False 处置 NaN。

    坍缩因子经广度面板投影回主面板时，掩码必须逐 (date, symbol) 保留；
    groupby.first() 跳过 NaN 会把未掩码值泄漏给已掩码 symbol。
    """

    dates = pd.date_range("2024-01-01", periods=4).strftime("%Y%m%d")
    flag = {"A": 0.0, "B": 1.0, "C": 1.0}

    def test_market_collapse_where_mask(self):
        main, breadth = _panels(self.dates, ["A", "B"], ["A", "B", "C"], self.flag)
        nodes = {"cf": {"expr": "mean(close_hfq)", "where": "flag > 0"}}
        p = plan.build_factor_plan(nodes, ["cf"])
        plan.materialize(main, breadth, p)
        # A 被 where 掩码 → NaN；B 保留均值 10.0
        assert main.loc[(self.dates[0], "A"), "cf"] != main.loc[(self.dates[0], "A"), "cf"]
        assert main.loc[(self.dates[-1], "A"), "cf"] != main.loc[(self.dates[-1], "A"), "cf"]
        assert main.loc[(self.dates[0], "B"), "cf"] == pytest.approx(10.0)
        # 未掩码 symbol 间仍同日同值（广播语义不变）
        unmasked = main["cf"][main["cf"].notna()]
        assert unmasked.groupby(level="trade_date").nunique().eq(1).all()

    def test_group_collapse_where_mask(self):
        main, breadth = _panels(self.dates, ["A", "B", "C"], ["A", "B", "C"],
                                self.flag)
        industry = {"A": "I1", "B": "I1", "C": "I2"}
        for df in (main, breadth):
            df["industry"] = df.index.get_level_values("symbol").map(industry)
        nodes = {"cg": {"expr": "group_mean(close_hfq, industry)",
                        "where": "flag > 0"}}
        p = plan.build_factor_plan(nodes, ["cg"])
        plan.materialize(main, breadth, p)
        # A 被 where 掩码 → NaN（组值不得经 first() 泄漏回 A）
        assert main.loc[(self.dates[0], "A"), "cg"] != main.loc[(self.dates[0], "A"), "cg"]
        # B（同组未掩码）保留组均值；C 保留 I2 组均值
        assert main.loc[(self.dates[0], "B"), "cg"] == pytest.approx(10.0)
        assert main.loc[(self.dates[0], "C"), "cg"] == pytest.approx(10.0)
