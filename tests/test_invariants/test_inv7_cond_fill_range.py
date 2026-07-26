"""INV7: Condition fill price must be within [low, high] of the day."""

from btcore.match.conditions import handle_stop_loss
from btcore.types import Holding


def test_inv7_stop_loss_fill_range_open_gap():
    """OPEN <= stop_price → fill at OPEN (which is >= LOW)."""
    holding = Holding(
        symbol="TEST.SH", shares=100, entry_date="20240101",
        entry_price=10.0, cost=1000.0, last_price=8.0, locked=False,
    )
    cond = {"type": "STOP_LOSS", "price": 8.5}
    bar = {"open": 8.0, "high": 8.5, "low": 7.8, "close": 8.2}

    executed, fill_price, _ = handle_stop_loss(holding, cond, bar)
    assert executed, "Should trigger when open <= stop_price"
    assert 7.8 <= fill_price <= 8.5, (
        f"INV7 FAILED: fill_price={fill_price} not in [7.8, 8.5]"
    )


def test_inv7_stop_loss_fill_range_intraday():
    """LOW <= stop_price < OPEN → fill at stop_price."""
    holding = Holding(
        symbol="TEST.SH", shares=100, entry_date="20240101",
        entry_price=10.0, cost=1000.0, last_price=9.0, locked=False,
    )
    cond = {"type": "STOP_LOSS", "price": 8.58}
    bar = {"open": 9.0, "high": 9.2, "low": 8.5, "close": 8.7}

    executed, fill_price, _ = handle_stop_loss(holding, cond, bar)
    assert executed, "Should trigger when low <= stop_price"
    assert 8.5 <= fill_price <= 9.2, (
        f"INV7 FAILED: fill_price={fill_price} not in [8.5, 9.2]"
    )
    assert fill_price == 8.58, f"INV7 FAILED: expected stop price 8.58, got {fill_price}"


def test_inv7_stop_loss_no_trigger():
    """Stop price below LOW → not triggered."""
    holding = Holding(
        symbol="TEST.SH", shares=100, entry_date="20240101",
        entry_price=10.0, cost=1000.0, last_price=9.0, locked=False,
    )
    cond = {"type": "STOP_LOSS", "price": 8.0}
    bar = {"open": 9.0, "high": 9.2, "low": 8.9, "close": 9.1}

    executed, _, _ = handle_stop_loss(holding, cond, bar)
    assert not executed, "Should not trigger when stop < low"
