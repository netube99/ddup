"""INV6: Corporate action consistency - shares × price conserved through 送股."""

import math

from btcore import corporate
from btcore.types import Account, Holding


def test_inv6_corp_consistency_stk_div():
    """After 送股, shares_before × close_before == shares_after × close_after."""
    account = Account(cash=1_000_000, initial_capital=1_000_000, slippage_ticks=0)

    holding = Holding(
        symbol="920478.BJ",
        shares=300,
        entry_date="20240520",
        entry_price=10.0,
        cost=3000.0,
        last_price=10.0,
        locked=False,
    )
    account.holdings["920478.BJ"] = holding

    today = "20240603"
    day_bars = {
        "920478.BJ": {
            "symbol": "920478.BJ",
            "trade_date": today,
            "open": 10.0,
            "close": 10.0,
            "high": 10.0,
            "low": 10.0,
            "pre_close": 10.0,
            "open_hfq": 10.0,
            "close_hfq": 10.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    }

    dividends = {"920478.BJ": {"stk_div": 0.4, "cash_div": 0.0}}

    class StubProvider:
        def get_dividends_on_date(self, date_str):
            return dividends if date_str == today else {}

    shares_before = holding.shares
    close_before = holding.last_price

    corporate.adjust(account, today, day_bars, StubProvider(), [])

    shares_after = holding.shares
    expected_shares = int(300 * (1 + 0.4))
    assert shares_after == expected_shares, (
        f"INV6 shares mismatch: {shares_after} != {expected_shares}"
    )

    # Market value conservation: shares_before × close_before ≈ shares_after × close_after
    value_before = shares_before * close_before
    value_after = shares_after * holding.last_price
    assert math.isclose(value_before, value_after, rel_tol=1e-10), (
        f"INV6 FAILED: value_before={value_before}, value_after={value_after}"
    )


def test_inv6_corp_cash_div_holding_cost():
    """After cash dividend, cost should decrease by net dividend."""
    account = Account(cash=1_000_000, initial_capital=1_000_000, slippage_ticks=0)

    holding = Holding(
        symbol="TEST.SH",
        shares=1000,
        entry_date="20240101",
        entry_price=10.0,
        cost=10000.0,
        last_price=10.0,
        locked=False,
    )
    account.holdings["TEST.SH"] = holding

    today = "20240603"
    day_bars = {"TEST.SH": {"symbol": "TEST.SH", "trade_date": today, "close": 10.0}}

    dividends = {"TEST.SH": {"stk_div": 0.0, "cash_div": 0.5}}

    class StubProvider:
        def get_dividends_on_date(self, date_str):
            return dividends if date_str == today else {}

    old_cash = account.cash
    old_cost = holding.cost

    corporate.adjust(account, today, day_bars, StubProvider(), [])

    gross = 0.5 * 1000  # 500
    # holding days: 20240603 - 20240101 = 154 days → ≤365 → 10% tax
    net = gross * 0.9  # 450
    assert account.cash == old_cash + net, (
        f"INV6 cash: {account.cash} != {old_cash + net}"
    )
    assert holding.cost == max(0, old_cost - net), (
        f"INV6 cost: {holding.cost} != {max(0, old_cost - net)}"
    )


def test_inv6_combined_stk_cash_event_identity():
    """INV-01/INV6: 同日送转+现金分红后账户恒等式成立（仅税负耗散）。

    fixture 920089.BJ 20240613（10送4派1.2，税 20%，74600 股）：
    送转后总市值 + 净派现 + 红利税 == 除权前总市值。
    """
    account = Account(cash=0.0, initial_capital=988_450.0, slippage_ticks=0)
    holding = Holding(
        symbol="920089.BJ",
        shares=74_600,
        entry_date="20240602",
        entry_price=13.25,
        cost=13.25 * 74_600,
        last_price=13.25,
        locked=False,
    )
    account.holdings["920089.BJ"] = holding

    today = "20240613"
    day_bars = {
        "920089.BJ": {
            "symbol": "920089.BJ", "trade_date": today,
            "open": 9.48, "close": 9.05, "pre_close": 9.38,
        }
    }
    dividends = {"920089.BJ": {"stk_div": 0.4, "cash_div": 0.12}}

    class StubProvider:
        def get_dividends_on_date(self, date_str):
            return dividends if date_str == today else {}

    corporate.adjust(account, today, day_bars, StubProvider(), [])

    assert holding.shares == int(74_600 * 1.4) == 104_440
    expected_price = 13.25 * 9.38 / (9.38 * 1.4 + 0.12)
    assert math.isclose(holding.last_price, expected_price, rel_tol=1e-12), (
        f"INV6 combined scale: {holding.last_price} != {expected_price}"
    )
    tax = 0.12 * 74_600 * 0.20
    value_before = 74_600 * 13.25
    value_after = account.cash + holding.shares * holding.last_price
    # 无舍入时组合除权严格守恒；fixture 的 pre_close=9.38 是交易所按 tick
    # 舍入的参考价（13.13/1.4=9.37857），1e-5 容差吸收该舍入残差
    assert math.isclose(value_after + tax, value_before, rel_tol=1e-5), (
        f"INV6 identity FAILED: value_after={value_after}, tax={tax}, "
        f"value_before={value_before}"
    )
    assert math.isclose(account.cash, 7161.60, rel_tol=1e-12)
