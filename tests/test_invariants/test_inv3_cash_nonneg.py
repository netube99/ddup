"""INV3: Cash non-negative - account.cash must never go below 0."""

from btcore.database import init_backtest_db
from tests.test_invariants.conftest import AccumulateBuyStrategy


def test_inv3_cash_nonneg(make_engine):
    strategy = AccumulateBuyStrategy()
    engine, calendar = make_engine(strategy, "20240610", max_positions=5)

    engine._compute_pending(calendar[0])

    conn = init_backtest_db(":memory:")
    try:
        for today in calendar:
            if today not in engine.bars_by_date:
                continue
            engine.step(today, engine.bars_by_date[today], conn)
            assert engine.account.cash >= -1e-6, (
                f"INV3 FAILED at {today}: cash={engine.account.cash}"
            )
    finally:
        conn.close()
