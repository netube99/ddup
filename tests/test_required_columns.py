"""数据契约强校验：bars 缺必需列时引擎 preload 直接报错（不走兜底派生）。"""

import pytest

from btcore.engine import Engine
from btcore.provider import DataProvider
from tests.test_foreign_backend import ForeignBackend, SentimentStrategy


class _DropColumnBackend(ForeignBackend):
    def __init__(self, column):
        self._column = column
        super().__init__()

    def query_bars(self, symbols, start, end, columns=None):
        return super().query_bars(symbols, start, end).drop(columns=[self._column])


@pytest.mark.parametrize(
    "column", ["pre_close", "up_limit", "down_limit", "vol", "adj_factor"]
)
def test_missing_required_column_raises(column):
    provider = DataProvider(_DropColumnBackend(column))
    engine = Engine(SentimentStrategy(), provider, initial_capital=1_000_000,
                    db_path=":memory:", max_positions=3)
    with pytest.raises(ValueError, match=column):
        engine.run("20240101", "20240110")


def test_full_contract_columns_run():
    """必需列齐全时 *_hfq / pct_chg 由引擎精确派生，回测正常。"""
    provider = DataProvider(ForeignBackend())
    engine = Engine(SentimentStrategy(), provider, initial_capital=1_000_000,
                    db_path=":memory:", max_positions=3)
    result = engine.run("20240101", "20240110")
    assert len(result["account_daily"]) > 0
