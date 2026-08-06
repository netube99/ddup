import pandas as pd
import pytest

from btcore.stats import calculate_statistics

_TRADE_COLS = ["date", "symbol", "side", "trigger", "price", "shares", "turnover",
               "commission", "stamp_tax", "transfer_fee", "slippage_amount",
               "net_amount", "reason"]


def make_account_daily(values, n_holdings=None, initial=1_000_000.0):
    dates = [f"202406{3 + i:02d}" for i in range(len(values))]
    data = {
        "date": dates,
        "total_value": values,
        "initial_capital": [initial] * len(values),
    }
    if n_holdings is not None:
        data["n_holdings"] = n_holdings
    return pd.DataFrame(data)


def make_trades(rows):
    return pd.DataFrame(rows, columns=_TRADE_COLS)


SAMPLE_TRADES = [
    ["20240603", "AAA", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
     -10007.1, ""],
    ["20240603", "BBB", "BUY", "MANUAL", 20.0, 1000, 20000.0, 10.0, 0.0, 0.2, 4.0,
     -20014.2, ""],
    ["20240605", "AAA", "SELL", "MANUAL", 12.0, 1000, 12000.0, 6.0, 12.0, 0.1, 3.0,
     11978.9, ""],
]


def test_trading_friction_values():
    adf = make_account_daily(
        [1_000_000.0, 1_010_000.0, 1_005_000.0, 1_020_000.0, 1_030_000.0],
        n_holdings=[0, 2, 2, 3, 3],
    )
    stats = calculate_statistics(adf, make_trades(SAMPLE_TRADES))
    fr = stats["trading_friction"]

    total_cost = 5.0 + 10.0 + 6.0 + 12.0 + 0.4 + 9.0  # 佣金+印花税+过户+滑点
    assert fr["total_cost"] == pytest.approx(total_cost)
    assert fr["cost_per_trade"] == pytest.approx(total_cost / 3)
    assert fr["cost_pct_of_turnover"] == pytest.approx(total_cost / 42000.0)
    assert fr["slippage_share"] == pytest.approx(9.0 / total_cost)
    assert fr["no_cost_total_return"] == pytest.approx(
        (1_030_000.0 + total_cost) / 1_000_000.0 - 1.0
    )
    # 磨损拖累为正，无摩擦收益高于实际收益
    assert fr["annualized_cost_drag"] > 0
    assert fr["no_cost_total_return"] > stats["total_return"]
    # AAA 一笔盈利 round trip: 11978.9 - 10007.1 = 1971.8（含费用口径 CONS-01）
    assert fr["cost_pct_of_gross_profit"] == pytest.approx(total_cost / 1971.8)


def test_management_complexity_values():
    adf = make_account_daily(
        [1_000_000.0, 1_010_000.0, 1_005_000.0, 1_020_000.0, 1_030_000.0],
        n_holdings=[0, 2, 2, 3, 3],
    )
    stats = calculate_statistics(adf, make_trades(SAMPLE_TRADES))
    mc = stats["management_complexity"]

    assert mc["max_positions"] == 3
    assert mc["avg_trades_per_day"] == pytest.approx(3 / 5)
    assert mc["avg_trades_per_active_day"] == pytest.approx(3 / 2)
    assert mc["max_trades_per_day"] == 2
    assert mc["active_day_ratio"] == pytest.approx(2 / 5)
    assert mc["avg_buy_amount"] == pytest.approx(15000.0)
    assert mc["min_buy_amount"] == pytest.approx(10000.0)
    expected_apv = (
        1_010_000.0 / 2 + 1_005_000.0 / 2 + 1_020_000.0 / 3 + 1_030_000.0 / 3
    ) / 4
    assert mc["avg_position_value"] == pytest.approx(expected_apv)


def test_round_trip_through_stk_div():
    """送转后卖出：lot 缩放、成本下摊，盈亏与含费用口径一致（CONS-01）。

    100 股 @10（含费成本 1007.1）→ 10送10（STK_DIV shares=200）→ 卖 200 @5.1
    （含费净额 1013.39）：pnl = 1013.39 - 1007.1 = +6.29；
    不含费用口径为 200×5.1 - 100×10 = +20，两者差 = 买卖费用合计 13.71。
    """
    adf = make_account_daily(
        [1_000_000.0, 1_010_000.0, 1_010_020.0], n_holdings=[1, 1, 0]
    )
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 100, 1000.0, 5.0, 0.0, 0.1, 2.0,
         -1007.1, ""],
        ["20240604", "AAA", "STK_DIV", "CORPORATE", 0.0, 200, 0.0, 0.0, 0.0, 0.0, 0.0,
         0.0, "stk_div"],
        ["20240605", "AAA", "SELL", "MANUAL", 5.1, 200, 1020.0, 5.0, 0.51, 0.1, 1.0,
         1013.39, ""],
    ])
    stats = calculate_statistics(adf, trades)
    rt = stats["round_trip"]
    assert rt["summary"]["total_realized_pnl"] == pytest.approx(6.29, abs=0.01)
    assert rt["trip_detail"][0]["shares"] == 200
    assert rt["trip_detail"][0]["pnl"] == pytest.approx(6.29, abs=0.01)


def test_round_trip_same_day_div_before_sell():
    """除息日清仓：同日 DIV 必须先于 SELL 入账，否则分红整体丢失。

    2026-08-03 实证（V4-F1）：旧排序仅按 date（同日 SELL 先于 DIV），
    champion 全周期 total_dividend_received 低估 -63%（13 笔同日清仓分红丢失）。
    100 股 @10 → 同日 DIV 50 元 + SELL 100 股 @10.5：
    修复后 trip 必须计入分红 50（total_dividend_received=50）。
    """
    adf = make_account_daily(
        [1_000_000.0, 1_005_000.0, 1_005_050.0], n_holdings=[1, 0, 0]
    )
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 100, 1000.0, 5.0, 0.0, 0.1, 2.0,
         -1007.1, ""],
        # 模拟落库顺序：SELL 行在 DIV 行之前（旧排序丢分红的复现条件）
        ["20240605", "AAA", "SELL", "MANUAL", 10.5, 100, 1050.0, 5.0, 0.525, 0.1, 1.0,
         1043.375, ""],
        ["20240605", "AAA", "DIV", "CORPORATE", 0.0, 0, 0.0, 0.0, 0.0, 0.0, 0.0,
         50.0, "cash_div"],
    ])
    stats = calculate_statistics(adf, trades)
    rt = stats["round_trip"]
    assert rt["summary"]["total_dividend_received"] == pytest.approx(50.0, abs=0.01)
    # 分红计入后含费用总盈亏 = 卖出净额 1043.375 - 买入含费成本 1007.1 + 分红 50
    assert rt["summary"]["total_realized_pnl"] == pytest.approx(86.275, abs=0.01)


def test_empty_trades_zero_dicts():
    adf = make_account_daily([1_000_000.0, 1_010_000.0], n_holdings=[0, 1])
    stats = calculate_statistics(adf, make_trades([]))
    fr = stats["trading_friction"]
    mc = stats["management_complexity"]
    assert set(fr) == {
        "total_cost", "cost_per_trade", "cost_pct_of_turnover",
        "annualized_cost_drag", "cost_pct_of_gross_profit",
        "no_cost_total_return", "slippage_share",
    }
    assert all(v == 0.0 for v in fr.values())
    assert mc["max_positions"] == 1
    assert mc["max_trades_per_day"] == 0
    assert mc["avg_trades_per_day"] == 0.0


def test_missing_n_holdings_degrades():
    adf = make_account_daily([1_000_000.0, 1_010_000.0])  # 无 n_holdings 列
    stats = calculate_statistics(adf, make_trades(SAMPLE_TRADES))
    mc = stats["management_complexity"]
    assert mc["max_positions"] == 0
    assert mc["avg_position_value"] == 0.0
    assert mc["max_trades_per_day"] == 2


def test_sell_source_attribution():
    """按卖出 trigger 分组 round-trip 归因。"""
    trades = SAMPLE_TRADES + [
        ["20240604", "CCC", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
         -10007.1, ""],
        ["20240606", "CCC", "SELL", "TRAILING_TP", 9.0, 1000, 9000.0, 5.0, 9.0, 0.1, 2.0,
         8983.9, "移动止盈"],
        ["20240607", "BBB", "SELL", "STOP_LOSS", 22.0, 1000, 22000.0, 6.0, 22.0, 0.2,
         4.0, 21968.0, "止损"],
    ]
    adf = make_account_daily(
        [1_000_000.0, 1_010_000.0, 1_005_000.0, 1_020_000.0, 1_030_000.0],
        n_holdings=[0, 2, 2, 3, 3],
    )
    stats = calculate_statistics(adf, make_trades(trades))
    src = stats["sell_source"]

    assert set(src) == {"MANUAL", "TRAILING_TP", "STOP_LOSS"}
    # CONS-01 含费用口径：AAA: 11978.9-10007.1=+1971.8; CCC: 8983.9-10007.1=-1023.2;
    # BBB: 21968.0-20014.2=+1953.8
    assert src["MANUAL"]["count"] == 1
    assert src["MANUAL"]["total_pnl"] == pytest.approx(1971.8)
    assert src["MANUAL"]["win_rate"] == 1.0
    assert src["TRAILING_TP"]["total_pnl"] == pytest.approx(-1023.2)
    assert src["TRAILING_TP"]["win_rate"] == 0.0
    assert src["STOP_LOSS"]["total_pnl"] == pytest.approx(1953.8)
    assert src["MANUAL"]["avg_holding_days"] == pytest.approx(2.0)


def test_sell_source_empty():
    adf = make_account_daily([1_000_000.0, 1_010_000.0], n_holdings=[0, 1])
    stats = calculate_statistics(adf, make_trades([]))
    assert stats["sell_source"] == {}


def test_round_trip_partial_sell_fee_allocation():
    """CONS-01：部分卖出按比例分摊买卖费用，剩余 lot 成本同步扣减。

    买入 1000 股含费成本 10007.1；分两次卖出 400/600：
    每次 pnl = 卖出净额比例分摊 - 成本比例分摊，总账 = 净额合计 - 含费成本。
    """
    adf = make_account_daily(
        [1_000_000.0, 1_005_000.0, 1_010_000.0], n_holdings=[1, 1, 0]
    )
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
         -10007.1, ""],
        ["20240605", "AAA", "SELL", "MANUAL", 12.0, 400, 4800.0, 5.0, 4.8, 0.1, 1.0,
         4789.1, ""],
        ["20240608", "AAA", "SELL", "MANUAL", 13.0, 600, 7800.0, 5.0, 7.8, 0.1, 1.0,
         7786.1, ""],
    ])
    stats = calculate_statistics(adf, trades)
    trips = stats["round_trip"]["trip_detail"]
    assert len(trips) == 2
    assert trips[0]["shares"] == 400
    assert trips[1]["shares"] == 600
    # 第一笔卖出 400 股 = 该笔全部：卖出净额全取 4789.1，
    # 成本按 400/1000 分摊 10007.1×0.4
    assert trips[0]["pnl"] == pytest.approx(4789.1 - 10007.1 * 0.4, abs=0.01)
    assert trips[1]["pnl"] == pytest.approx(7786.1 - 10007.1 * 0.6, abs=0.01)
    # 总账恒等：全部净额 - 含费成本
    total = sum(t["pnl"] for t in trips)
    assert total == pytest.approx((4789.1 + 7786.1) - 10007.1, abs=0.01)


def test_round_trip_missing_net_amount_falls_back(caplog):
    """CONS-01 防御：缺 net_amount 的 trade_log 回退裸价口径并告警。"""
    adf = make_account_daily(
        [1_000_000.0, 1_010_000.0, 1_020_000.0], n_holdings=[1, 1, 0]
    )
    rows = [r[:] for r in SAMPLE_TRADES if r[1] == "AAA"]
    for r in rows:
        r[11] = None  # net_amount 缺失
    stats = calculate_statistics(adf, make_trades(rows))
    rt = stats["round_trip"]
    # 裸价回退：12000 - 10000 = 2000
    assert rt["summary"]["total_realized_pnl"] == pytest.approx(2000.0, abs=0.01)
    assert "缺 net_amount" in caplog.text


def test_open_position_cost_basis_includes_fees():
    """CONS-01：未平仓持仓 cost_basis 含费用（期末估值口径一致）。"""
    from types import SimpleNamespace

    adf = make_account_daily([1_000_000.0, 1_010_000.0], n_holdings=[1, 1])
    trades = make_trades([
        ["20240603", "AAA", "BUY", "MANUAL", 10.0, 1000, 10000.0, 5.0, 0.0, 0.1, 2.0,
         -10007.1, ""],
    ])
    stats = calculate_statistics(
        adf, trades, holdings={"AAA": SimpleNamespace(last_price=11.0)}
    )
    op = stats["round_trip"]["open_positions"][0]
    assert op["cost_basis"] == pytest.approx(10007.1, abs=0.01)
    # 浮盈 = 1000×11 - 含费成本 10007.1
    assert op["pnl"] == pytest.approx(11000.0 - 10007.1, abs=0.01)
