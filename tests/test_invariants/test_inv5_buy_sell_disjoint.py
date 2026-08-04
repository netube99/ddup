"""INV5: Same-day buy/sell disjoint - must not buy and sell same symbol."""

import pytest


class Inv5Strategy:
    """Returns conflicting buy/sell for testing INV5 rejection."""

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 5, "top_k": 30}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}
        symbols = list(bars.keys())
        if len(symbols) >= 2:
            return {"buy": [symbols[0], symbols[1]], "sell": [symbols[1]]}
        return {"buy": [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_inv5_buy_sell_disjoint_rejects(make_engine):
    strategy = Inv5Strategy()
    engine, calendar = make_engine(strategy, "20240610", max_positions=5)

    # compute_pending should raise because buy ∩ sell is non-empty
    with pytest.raises(ValueError, match="冲突"):
        engine.compute_pending(calendar[0])
