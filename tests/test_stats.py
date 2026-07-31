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
    # AAA 一笔盈利 round trip: 12000 - 10000 = 2000（不含费用口径）
    assert fr["cost_pct_of_gross_profit"] == pytest.approx(total_cost / 2000.0)


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
    # AAA: 12000-10000=+2000;CCC: 9000-10000=-1000;BBB: 22000-20000=+2000
    assert src["MANUAL"]["count"] == 1
    assert src["MANUAL"]["total_pnl"] == pytest.approx(2000.0)
    assert src["MANUAL"]["win_rate"] == 1.0
    assert src["TRAILING_TP"]["total_pnl"] == pytest.approx(-1000.0)
    assert src["TRAILING_TP"]["win_rate"] == 0.0
    assert src["STOP_LOSS"]["total_pnl"] == pytest.approx(2000.0)
    assert src["MANUAL"]["avg_holding_days"] == pytest.approx(2.0)


def test_sell_source_empty():
    adf = make_account_daily([1_000_000.0, 1_010_000.0], n_holdings=[0, 1])
    stats = calculate_statistics(adf, make_trades([]))
    assert stats["sell_source"] == {}
