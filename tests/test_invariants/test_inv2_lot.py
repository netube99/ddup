"""INV2: Lot size - all holding shares must be multiples of 100."""

from btcore.database import init_backtest_db
from tests.test_invariants.conftest import AccumulateBuyStrategy


def test_inv2_lot_size(make_engine):
    strategy = AccumulateBuyStrategy()
    engine, calendar = make_engine(strategy, "20240607",
                                   initial_capital=2_000_000, max_positions=5)

    engine.compute_pending(calendar[0])

    conn = init_backtest_db(":memory:")
    try:
        for today in calendar:
            if today not in engine.bars_by_date:
                continue
            engine.step(today, engine.bars_by_date[today], conn)
    finally:
        conn.close()

    for sym, h in engine.account.holdings.items():
        assert h.shares % 100 == 0, (
            f"INV2 FAILED: {sym} shares={h.shares} not a multiple of 100"
        )
