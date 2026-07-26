"""INV1: Accounting identity - cash + holdings_market_value == total_value."""

import math

from btcore.database import init_backtest_db


class Inv1Strategy:
    """Buys 1 stock on day 1, no sells."""

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 10, "top_k": 30}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}
        sym = list(bars.keys())[0]
        return {"buy": [sym], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_inv1_account_identity(make_engine):
    strategy = Inv1Strategy()
    engine, calendar = make_engine(strategy, "20240607", max_positions=10)
    calendar = calendar[:2]

    engine._compute_pending(calendar[0])

    conn = init_backtest_db(":memory:")
    try:
        for today in calendar:
            if today not in engine.bars_by_date:
                continue
            engine.step(today, engine.bars_by_date[today], conn)
    finally:
        conn.close()

    acct = engine.account
    holdings_value = sum(
        h.shares * h.last_price for h in acct.holdings.values()
    )
    computed = acct.cash + holdings_value
    assert math.isclose(computed, acct.total_value, rel_tol=1e-6), (
        f"INV1 FAILED: total_value={acct.total_value}, "
        f"cash+holdings={computed}, diff={abs(computed - acct.total_value)}"
    )
