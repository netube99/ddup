"""黄金库刷新脚本的纯函数回归：cn_m 窗口拼接与 ETF 0.001 档位限价。"""

import pytest

from scripts.refresh_gold_db import _cn_m_window, _etf_limit


def test_cn_m_window_full_years():
    assert _cn_m_window(2003, 2026) == ("200301", "202612")
    assert _cn_m_window(2026, 2026) == ("202601", "202612")


@pytest.mark.parametrize("pre_close,factor,expected", [
    (2.708, "1.1", 2.979),
    (2.708, "0.9", 2.437),
    (11.009, "1.1", 12.11),
    (11.009, "0.9", 9.908),
    (11.513, "1.1", 12.664),
    (11.513, "0.9", 10.362),
    (10.334, "1.1", 11.367),
    (10.334, "0.9", 9.301),
])
def test_etf_limit_mill_rounding(pre_close, factor, expected):
    assert _etf_limit(pre_close, factor) == pytest.approx(expected)
