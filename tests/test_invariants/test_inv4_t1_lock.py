"""INV4: T+1 lock - new buys locked during same-day condition check, unlocked after step."""

from btcore import limits
from btcore.costs import calc_trade_costs
from btcore.database import init_backtest_db
from btcore.match.conditions import exit_conditions
from btcore.slippage import apply_slippage
from tests.conftest import make_account, make_bar, make_holding


class Inv4Strategy:
    """Buys on day 1 to test T+1 locking."""

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 1, "top_k": 30}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}
        current = set(snapshot.holdings.keys())
        candidates = [s for s in bars if s not in current]
        return {"buy": [candidates[0]] if candidates else [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_inv4_t1_lock_unlocks_after_step(make_engine):
    """After engine.step(), compute_pending runs and unlocks all holdings."""
    strategy = Inv4Strategy()
    engine, calendar = make_engine(strategy, "20240610", max_positions=1)
    calendar = calendar[:2]

    engine.compute_pending(calendar[0])

    conn = init_backtest_db(":memory:")
    try:
        engine.step(calendar[0], engine.bars_by_date[calendar[0]], conn)
        holdings = list(engine.account.holdings.values())
        assert len(holdings) > 0, "Expected at least one holding after buy"

        # After step(), compute_pending has run and unlocked holdings
        for h in holdings:
            assert not h.locked, (
                f"INV4 FAILED: holding should be unlocked after step, "
                f"got locked={h.locked}"
            )
    finally:
        conn.close()


def test_inv4_t1_lock_skips_condition_on_same_day():
    """On day of buy, locked=True holdings should be skipped by condition check."""
    account = make_account(cash=1_000_000.0, slippage_ticks=0)

    # Simulate a locked holding (just bought today)
    account.holdings["TEST.SH"] = make_holding(
        symbol="TEST.SH", shares=500, entry_date="20240603", locked=True,
        conditions=[{"type": "STOP_LOSS", "price": 9.0}],
    )

    bars = {
        "TEST.SH": make_bar(open=8.0, high=9.0, low=7.5, close=8.0,
                            pre_close=10.0, up_limit=11.0, down_limit=9.0,
                            symbol="TEST.SH"),
    }

    trades = exit_conditions(account, bars, limits.get_limit_prices,
                             calc_trade_costs, apply_slippage)
    # Locked holding should be skipped even though stop_loss triggered
    assert len(trades) == 0, (
        "INV4 FAILED: locked holding should not trigger condition trade"
    )
    assert "TEST.SH" in account.holdings, "Holding should not be deleted"
