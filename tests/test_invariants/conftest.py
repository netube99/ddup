"""INV 测试共享件：手动步进引擎搭建 fixture + INV2/INV3 共用策略。"""

import pytest

from btcore.engine import Engine
from btcore.provider import DataProvider
from tests.conftest import MockDataBackend


class AccumulateBuyStrategy:
    """每日买入未持仓标的（至多 max_positions 只），只买不卖。"""

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 5, "top_k": 30}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}
        current = set(snapshot.holdings.keys())
        candidates = [s for s in bars if s not in current]
        return {"buy": candidates[:self.config["max_positions"]], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


@pytest.fixture
def make_engine():
    """搭建手动步进引擎：预载 bars → 排序 → 日历 → Engine → bars_by_date → on_start。

    返回 (engine, calendar)。调用方在循环前调一次
    _compute_pending(calendar[0])，之后由 step() 结尾接力调用，
    与 engine.run() 的真实时序一致（select 每日恰好一次）。
    """
    def _make(strategy, calendar_end, initial_capital=1_000_000, max_positions=10):
        provider = DataProvider(MockDataBackend())
        bars_df = provider.get_engine_bars(None, "20240701")
        bars_df.sort_index(inplace=True)
        calendar = provider.get_calendar("20240603", calendar_end)
        engine = Engine(strategy, provider, initial_capital=initial_capital,
                        db_path=":memory:", max_positions=max_positions)
        engine.bars_df = bars_df
        engine.bars_by_date = {
            d: group.droplevel("trade_date")
            for d, group in bars_df.groupby(level="trade_date", sort=False)
        }
        strategy.on_start(provider, calendar[0])
        return engine, calendar
    return _make
