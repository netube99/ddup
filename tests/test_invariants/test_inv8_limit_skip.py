"""INV8: Limit skip - no BUY when open==limit_up, no SELL when open==limit_down."""

from btcore import limits
from btcore.costs import calc_trade_costs
from btcore.match.manual import manual_buy, manual_sell
from btcore.slippage import apply_slippage
from btcore.types import Account, Holding


def test_inv8_no_buy_at_limit_up():
    """When open == limit_up, manual_buy should skip."""
    account = Account(cash=1_000_000, initial_capital=1_000_000, slippage_ticks=0)

    bars = {
        "TEST.SH": {
            "symbol": "TEST.SH",
            "trade_date": "20240603",
            "open": 11.0,
            "close": 11.0,
            "high": 11.0,
            "low": 11.0,
            "pre_close": 10.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    }

    trades = manual_buy(
        account, bars, ["TEST.SH"], 10,
        limits.get_limit_prices, calc_trade_costs, apply_slippage,
    )
    assert len(trades) == 0, (
        f"INV8 FAILED: Should not produce BUY at limit_up, got {len(trades)} trades"
    )


def test_inv8_no_sell_at_limit_down():
    """When open == limit_down, manual_sell should skip."""
    account = Account(cash=1_000_000, initial_capital=1_000_000, slippage_ticks=0)
    holding = Holding(
        symbol="TEST.SH", shares=1000, entry_date="20240101",
        entry_price=10.0, cost=10000.0, last_price=9.0, locked=False,
    )
    account.holdings["TEST.SH"] = holding

    bars = {
        "TEST.SH": {
            "symbol": "TEST.SH",
            "trade_date": "20240603",
            "open": 9.0,
            "close": 9.0,
            "high": 9.0,
            "low": 9.0,
            "pre_close": 10.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    }

    trades = manual_sell(
        account, bars, ["TEST.SH"],
        limits.get_limit_prices, calc_trade_costs, apply_slippage,
    )
    assert len(trades) == 0, (
        f"INV8 FAILED: Should not produce SELL at limit_down, got {len(trades)} trades"
    )


def test_inv8_buy_allowed_below_limit_up():
    """When open < limit_up, buy should proceed."""
    account = Account(cash=1_000_000, initial_capital=1_000_000, slippage_ticks=0)

    bars = {
        "TEST.SH": {
            "symbol": "TEST.SH",
            "trade_date": "20240603",
            "open": 10.5,
            "close": 10.5,
            "high": 10.5,
            "low": 10.5,
            "pre_close": 10.0,
            "up_limit": 11.0,
            "down_limit": 9.0,
        }
    }

    trades = manual_buy(
        account, bars, ["TEST.SH"], 10,
        limits.get_limit_prices, calc_trade_costs, apply_slippage,
    )
    assert len(trades) == 1, "Should produce BUY when open < limit_up"
    assert trades[0].side == "BUY"
    assert trades[0].symbol == "TEST.SH"


def test_inv8_nan_limits_fall_back_to_plate_rules():
    """LEFT JOIN 缺行产生 NaN 涨跌停 → 回退板块规则推算, 不穿透守卫。"""
    bar = {
        "up_limit": float("nan"), "down_limit": float("nan"), "pre_close": 10.0,
    }
    up, down = limits.get_limit_prices("TEST.SH", bar, "20240603")
    assert up == 11.0
    assert down == 9.0


def test_inv8_nan_preclose_returns_none():
    """pre_close 为 NaN（数据首日 shift 产生）→ 无法判定 → (None, None)。"""
    bar = {
        "up_limit": float("nan"), "down_limit": float("nan"),
        "pre_close": float("nan"),
    }
    up, down = limits.get_limit_prices("TEST.SH", bar, "20240603")
    assert up is None
    assert down is None
