"""前视防护门面 — 引擎和策略只通过本层获取 bar 数据。

两个入口:
  get_engine_bars      — 含当日, 引擎撮合用
  get_historical_bars  — 不含当日, 策略/因子用

其他数据 (ST、行业、基准等) 通过 .backend 直接访问,
方法由用户在自己的后端类上自行定义, 鸭子类型调用, 不涉及前视防护。
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd

from btcore.backend import DataBackend


class DataProvider:
    """前视防护门面（约定性防护，非强制）。

    策略代码通过 get_historical_bars 获取 bar 数据，默认路径看不到
    当日及未来行情；.backend 直调不受限，防护靠使用约定而非拦截。
    """

    def __init__(self, backend: DataBackend):
        self.backend = backend
        self._prev_day_cache: dict[str, str | None] = {}
        self._bars_df: pd.DataFrame | None = None
        self._as_of_date: str | None = None

    # ── 引擎用 (含当日) ──

    def get_engine_bars(
        self,
        symbols: list[str] | None,
        trade_date: str,
        lookback_start: str | None = None,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        return self.backend.query_bars(
            symbols, lookback_start or "00000101", trade_date, columns=columns,
        )

    # ── 策略用 (不含当日) ──

    def attach_bars(self, bars_df: pd.DataFrame) -> None:
        """引擎 preload 后注入全量 bars（内部接线，非策略接口）。

        bars_df 必须已按 MultiIndex 排序（引擎在 preload 后 sort_index）。
        注入后 get_historical_bars 从预载数据本地切片，不再回源 SQL。
        """
        self._bars_df = bars_df

    def get_historical_bars(
        self,
        symbols: list[str] | None,
        end_date: str,
        lookback_days: int = 365,
    ) -> pd.DataFrame:
        """[end_date - lookback, 前一交易日] 的历史 bars，不含 end_date 当日。

        回测进行中 end_date 被钳制到当前模拟日，策略传未来日期也拿不到未来数据。
        已 attach_bars 时返回预载数据的只读切片（勿原地修改），否则回源 SQL。
        """
        if self._as_of_date is not None:
            end_date = min(end_date, self._as_of_date)
        prev = self._prev_trading_day(end_date)
        if prev is None:
            return pd.DataFrame()
        lookback_start = (
            date.fromisoformat(end_date) - timedelta(days=lookback_days)
        ).strftime("%Y%m%d")
        if self._bars_df is not None:
            sliced = self._bars_df.loc[lookback_start:prev]
            if symbols is not None:
                mask = sliced.index.get_level_values("symbol").isin(symbols)
                sliced = sliced[mask]
            return sliced
        return self.backend.query_bars(symbols, lookback_start, prev)

    # ── 透传 ──

    def get_calendar(self, start: str, end: str) -> list[str]:
        return self.backend.get_calendar(start, end)

    def get_dividends_on_date(self, date_str: str) -> dict:
        return self.backend.get_dividends_on_date(date_str)

    # ── 内部 ──

    def _prev_trading_day(self, date_str: str) -> str | None:
        if date_str in self._prev_day_cache:
            return self._prev_day_cache[date_str]
        lookback = (date.fromisoformat(date_str) - timedelta(days=30)).strftime("%Y%m%d")
        calendar = self.get_calendar(lookback, date_str)
        before = [d for d in calendar if d < date_str]
        prev = before[-1] if before else None
        self._prev_day_cache[date_str] = prev
        return prev
