"""btcore.strategy_tools schedule 测试：YAML schedule 键 → 调仓日包装 select。"""

import pytest

from btcore.provider import DataProvider
from btcore.strategy import Strategy
from btcore.strategy_loader import load_strategy
from btcore.strategy_tools import _rebalance_dates
from tests.conftest import MockDataBackend


class DummyStrategy(Strategy):
    """每天都想买同一只票，便于观察调度是否拦截。"""

    def on_start(self, provider, first_date: str, end_date: str | None = None):
        pass

    def select(self, bars, snapshot, provider) -> dict:
        return {"buy": ["000001.SZ"], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def _write(tmp_path, body: str) -> str:
    path = tmp_path / "s.yaml"
    path.write_text(body, encoding="utf-8")
    return str(path)


def _load_scheduled(tmp_path, schedule_yaml: str):
    return load_strategy(_write(tmp_path, f"""\
strategy: tests.test_scheduler:DummyStrategy
{schedule_yaml}"""))


def test_weekly_schedule_intercepts_select(tmp_path):
    strategy = _load_scheduled(tmp_path,
                               "schedule:\n  frequency: weekly\n  weekday: 1\n")
    provider = DataProvider(MockDataBackend())
    strategy.on_start(provider, "20240603", end_date="20240614")

    # 20240603 是周一（每周第 1 个交易日）→ 透传
    on_day = {"000001.SZ": {"trade_date": "20240603"}}
    assert strategy.select(on_day, None, provider) == {
        "buy": ["000001.SZ"], "sell": []}
    # 20240604 非调仓日 → 空名单
    off_day = {"000001.SZ": {"trade_date": "20240604"}}
    assert strategy.select(off_day, None, provider) == {"buy": [], "sell": []}


def test_daily_schedule_passthrough(tmp_path):
    strategy = _load_scheduled(tmp_path, "schedule:\n  frequency: daily\n")
    provider = DataProvider(MockDataBackend())
    strategy.on_start(provider, "20240603", end_date="20240614")

    day = {"000001.SZ": {"trade_date": "20240604"}}
    assert strategy.select(day, None, provider) == {
        "buy": ["000001.SZ"], "sell": []}


def test_no_end_date_no_wrap(tmp_path):
    strategy = _load_scheduled(tmp_path,
                               "schedule:\n  frequency: weekly\n  weekday: 1\n")
    provider = DataProvider(MockDataBackend())
    strategy.on_start(provider, "20240603", end_date=None)

    day = {"000001.SZ": {"trade_date": "20240604"}}
    assert strategy.select(day, None, provider) == {
        "buy": ["000001.SZ"], "sell": []}


def test_unknown_frequency_rejected(tmp_path):
    with pytest.raises(ValueError, match="frequency"):
        _load_scheduled(tmp_path, "schedule:\n  frequency: hourly\n")


def test_unknown_schedule_key_rejected(tmp_path):
    with pytest.raises(ValueError, match="未知 schedule 键"):
        _load_scheduled(tmp_path,
                        "schedule:\n  frequency: weekly\n  hour: 1\n")


def test_zero_weekday_rejected(tmp_path):
    with pytest.raises(ValueError, match="weekday"):
        _load_scheduled(tmp_path,
                        "schedule:\n  frequency: weekly\n  weekday: 0\n")


def test_rebalance_dates_weekly_negative_index():
    cal = ["20240603", "20240604", "20240605", "20240606", "20240607",
           "20240610", "20240611"]
    rule = {"frequency": "weekly", "weekday": -1}
    assert _rebalance_dates(cal, rule) == {"20240607", "20240611"}


def test_rebalance_dates_monthly():
    cal = ["20240530", "20240531", "20240603", "20240604"]
    rule = {"frequency": "monthly", "monthday": 1}
    assert _rebalance_dates(cal, rule) == {"20240530", "20240603"}
