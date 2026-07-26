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
