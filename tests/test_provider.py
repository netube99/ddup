"""DataProvider 本地切片与模拟日钳制。"""

import pandas as pd

from btcore.provider import DataProvider
from tests.conftest import MockDataBackend


def test_attached_slice_uses_local_data():
    """attach_bars 后 get_historical_bars 走本地切片而非回源 SQL。"""
    backend = MockDataBackend()
    provider = DataProvider(backend)
    bars = backend.query_bars(None, "20240603", "20240701").copy()
    bars.sort_index(inplace=True)  # 模拟引擎 preload 后的排序
    bars["close"] = -1.0  # 标记值：走 SQL fallback 拿不到
    provider.attach_bars(bars)

    sym = bars.index.get_level_values("symbol").unique()[0]
    hist = provider.get_historical_bars([sym], "20240610", lookback_days=7)

    assert not hist.empty
    assert (hist["close"] == -1.0).all(), "数据应来自 attach 的 DataFrame"
    assert set(hist.index.get_level_values("symbol")) == {sym}
    dates = hist.index.get_level_values("trade_date")
    assert dates.max() < "20240610", "不含 end_date 当日"


def test_as_of_clamps_future_end_date():
    """回测进行中策略传未来 end_date，结果被钳制到当前模拟日之前。"""
    backend = MockDataBackend()
    provider = DataProvider(backend)
    bars = backend.query_bars(None, "20240603", "20240701")
    bars.sort_index(inplace=True)
    provider.attach_bars(bars)
    provider.set_as_of("20240610")

    hist = provider.get_historical_bars(None, "20991231", lookback_days=365)

    dates = hist.index.get_level_values("trade_date")
    assert dates.max() < "20240610"
    assert dates.min() >= "20240603"


def test_sql_fallback_without_attach():
    """未 attach 时维持原 SQL 路径（独立使用 provider 的场景）。"""
    provider = DataProvider(MockDataBackend())

    hist = provider.get_historical_bars(["000001.SZ"], "20240610", lookback_days=7)

    assert not hist.empty
    dates = hist.index.get_level_values("trade_date")
    assert dates.max() < "20240610"


def test_benchmark_trend_with_data():
    """get_benchmark_trend 返回正确的累计收益。"""
    backend = MockDataBackend()
    provider = DataProvider(backend)
    provider.benchmark = "000300.SH"

    trend = provider.get_benchmark_trend("20240701", window=30)

    assert trend is not None
    assert isinstance(trend, float)
    assert -1.0 <= trend <= 1.0


def test_benchmark_trend_no_benchmark():
    """未配置 benchmark 时 get_benchmark_trend 返回 None。"""
    provider = DataProvider(MockDataBackend())
    # benchmark 未设置

    result = provider.get_benchmark_trend("20240701", window=30)

    assert result is None


# ── 基准列回退: 无 hfq_close 时用 close ──


class _CloseOnlyBenchBackend(MockDataBackend):
    """基准表只有 close 列（无 hfq_close）的后端。"""

    def get_benchmark_bars(
        self, code: str = "000300.SH", start: str = "", end: str = ""
    ):
        bm = self._benchmark.copy()
        if start:
            bm = bm[bm["trade_date"] >= start]
        if end:
            bm = bm[bm["trade_date"] <= end]
        bm = bm.copy()
        bm["trade_date"] = pd.to_datetime(bm["trade_date"])
        bm.set_index("trade_date", inplace=True)
        return bm[["close"]]


class _NoPriceBenchBackend(MockDataBackend):
    """基准表既无 hfq_close 也无 close（只有占位列）。"""

    def get_benchmark_bars(
        self, code: str = "000300.SH", start: str = "", end: str = ""
    ):
        bm = self._benchmark.copy()
        bm["trade_date"] = pd.to_datetime(bm["trade_date"])
        bm.set_index("trade_date", inplace=True)
        return bm.assign(dummy=1.0)[["dummy"]]


def test_benchmark_returns_falls_back_to_close():
    """基准表只有 close 列时不再 KeyError，按 close 计算收益。"""
    backend = _CloseOnlyBenchBackend()
    backend._benchmark = backend._benchmark.rename(
        columns={"hfq_close": "close"}
    )
    provider = DataProvider(backend)
    provider.benchmark = "000300.SH"

    rets = provider.get_benchmark_returns("20240701")

    assert rets is not None
    assert not rets.empty
    assert "20240604" in set(rets.index)  # 首日被 pct_change 丢弃, 从第二日算起


def test_benchmark_returns_none_without_price_cols():
    """基准表无任何价格列 → 返回 None 而非 KeyError。"""
    provider = DataProvider(_NoPriceBenchBackend())
    provider.benchmark = "000300.SH"

    assert provider.get_benchmark_returns("20240701") is None
