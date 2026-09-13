"""TDD 审查测试（scope: btcore/stats.py, database.py, generic_sql.py, adapters/）。

每个测试先 RED 后 GREEN；只允许证明性断言（written contract 依据见注释）。
"""

from types import SimpleNamespace

import pytest

from btcore.stats import calculate_statistics
from tests.test_stats import make_account_daily, make_trades


def test_symbol_contribution_open_position_matches_round_trip():
    """symbol_contribution.total_contribution 必须与 round_trip 同口径（CONS-01）。

    场景：买 1000 股含费成本 10007.1，部分卖出 400 股（净额 4789.1），
    剩余 600 股仍持仓（last=10, cost=6004.26，为引擎 apply_partial_sell 缩减后的账面成本）。
    该 symbol 的真实盈亏（= 现金流 + 期末持仓市值）：
        -10007.1 + 4789.1 + 6000 = 782.0
    也就是 trip pnl 786.26 + open pnl -4.26。旧实现把剩余持仓的含费成本
    6004.26 重复扣一次（realized 里扣全额买入成本，unrealized 再减账面成本），
    得到 -5222.26。
    """
    adf = make_account_daily([1_000_000.0, 1_005_000.0, 1_010_000.0],
                             n_holdings=[1, 1, 1])
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
         -10007.1, ""],
        ["20240605", "AAA", "SELL", "MANUAL", 12.0, 400, 4800.0, 5.0, 4.8, 0.1, 1.0,
         4789.1, ""],
    ])
    holdings = {"AAA": SimpleNamespace(shares=600, last_price=10.0, cost=6004.26)}
    stats = calculate_statistics(adf, trades, holdings=holdings)
    contrib = stats["symbol_contribution"]["AAA"]
    rt = stats["round_trip"]
    rt_total = (sum(t["pnl"] for t in rt["trip_detail"])
                + sum(p["pnl"] for p in rt["open_positions"]))
    assert contrib["total_contribution"] == pytest.approx(782.0, abs=0.01)
    assert contrib["total_contribution"] == pytest.approx(rt_total, abs=0.01)


def test_symbol_contribution_no_dividend_double_count():
    """现金分红在 symbol_contribution 中只能计入一次（CONS-01 净额口径）。

    场景：买 1000 股（含费成本 10007.1），除息分红 400 元入现金，
    引擎侧 holding.cost 被扣减为 9600（corporate._apply_cash_div），期末仍持仓。
    真实盈亏 = -10007.1 + 400 + 1000×10 = 392.9。
    旧实现 unrealized（MV-已扣分红成本=400）与 dividend_received(400) 重复，
    且 realized 再扣全额买入成本，合计 -9207.1。
    """
    adf = make_account_daily([1_000_000.0, 1_000_400.0, 1_000_400.0],
                             n_holdings=[1, 1, 1])
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
         -10007.1, ""],
        ["20240605", "AAA", "DIV", "CORPORATE", 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
         400.0, "cash_div"],
    ])
    holdings = {"AAA": SimpleNamespace(shares=1000, last_price=10.0, cost=9600.0)}
    stats = calculate_statistics(adf, trades, holdings=holdings)
    contrib = stats["symbol_contribution"]["AAA"]
    assert contrib["dividend_received"] == pytest.approx(400.0)
    assert contrib["total_contribution"] == pytest.approx(392.9, abs=0.01)


def test_symbol_contribution_fully_closed_unchanged():
    """回归锚：全部平仓的 symbol，total_contribution 与旧口径一致（现金流 + 分红）。"""
    adf = make_account_daily([1_000_000.0, 1_010_000.0, 1_020_000.0],
                             n_holdings=[1, 1, 0])
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
         -10007.1, ""],
        ["20240605", "AAA", "DIV", "CORPORATE", 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
         50.0, "cash_div"],
        ["20240606", "AAA", "SELL", "MANUAL", 12.0, 1000, 12000.0, 6.0, 12.0, 0.1, 3.0,
         11978.9, ""],
    ])
    stats = calculate_statistics(adf, trades, holdings={})
    contrib = stats["symbol_contribution"]["AAA"]
    assert contrib["realized_pnl"] == pytest.approx(11978.9 - 10007.1, abs=0.01)
    assert contrib["dividend_received"] == pytest.approx(50.0)
    assert contrib["total_contribution"] == pytest.approx(1971.8 + 50.0, abs=0.01)
