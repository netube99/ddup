"""前视防护门面 — 引擎和策略只通过本层获取 bar 与基准数据。

三个入口:
  get_engine_bars        — 含当日, 引擎撮合用
  get_historical_bars    — 不含当日, 策略/因子用
  get_benchmark_returns  — 基准指数日收益序列, 受前视保护

其他数据 (ST、行业、指数成分等) 通过 .backend 直接访问,
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
        self.benchmark: str | None = None  # 由 Engine 设置，策略无需感知基准代码
        self._prev_day_cache: dict[str, str | None] = {}
        self._bars_df: pd.DataFrame | None = None
        self._as_of_date: str | None = None
        # 基准指数面板精确键缓存：(code, start, end) → frame|None。
        # 只对同区间重复调用去重（如 trend + returns 同日连调）；逐日滑窗
        # 调用每窗回源一次——单标的索引查询，成本可忽略。
        # 长回测每日一窗，缓存有界增长（≈ 回测天数 × 1 键），刻意不清理
        self._bench_cache: dict[tuple[str, str, str], pd.DataFrame | None] = {}

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

    def set_as_of(self, date_str: str | None) -> None:
        """钳制查询锚点：get_historical_bars / get_benchmark_returns 的查询端上限。

        引擎在回测主循环每日更新；preload 阶段（get_universe / on_start）
        即已钳到首日前一交易日，钩子内传未来日期也拿不到未来数据。
        """
        self._as_of_date = date_str

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
        prev = self.prev_trading_day(end_date)
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

    # ── 基准指数 ──

    def get_benchmark_returns(
        self, end_date: str, lookback_days: int = 252,
    ) -> pd.Series | None:
        """返回基准指数日收益序列（后复权收盘价的 pct_change），受前视保护。

        end_date: 查询锚点日（YYYYMMDD）。回测中会钳制到当前模拟日。
        lookback_days: 回溯天数，默认 252（约一年）。

        返回: index 为 YYYYMMDD 字符串的日收益 Series（小数，非百分比）；
              未配置 benchmark / 后端无 get_benchmark_bars / 无数据时返回 None。

        前视保护: 数据截止于 end_date 的前一交易日，策略拿不到当日基准收益。
        """
        if not self.benchmark:
            return None
        bench_fn = getattr(self.backend, "get_benchmark_bars", None)
        if not callable(bench_fn):
            return None
        if self._as_of_date is not None:
            end_date = min(end_date, self._as_of_date)
        prev = self.prev_trading_day(end_date)
        if prev is None:
            return None
        lookback_start = (
            date.fromisoformat(end_date) - timedelta(days=lookback_days)
        ).strftime("%Y%m%d")
        key = (self.benchmark, lookback_start, prev)
        if key not in self._bench_cache:
            self._bench_cache[key] = bench_fn(self.benchmark, lookback_start, prev)
        bench = self._bench_cache[key]
        if bench is None or bench.empty:
            return None
        # 列口径与 engine benchmark_nav 提取一致：优先 hfq_close，缺列回退
        # close（后端基准表可能只提供裸价）；两列都没有则视为无基准数据
        col = ("hfq_close" if "hfq_close" in bench.columns
               else "close" if "close" in bench.columns else None)
        if col is None:
            return None
        ret = bench[col].pct_change()
        ret.index = pd.Index(pd.to_datetime(ret.index).strftime("%Y%m%d"))
        return ret.dropna()

    def get_benchmark_trend(self, end_date: str, window: int = 30) -> float | None:
        """返回基准指数近 window 日累计收益（前视保护）。

        end_date 钳制到当前模拟日。window 默认 30。

        Returns:
            累计收益小数（如 0.05 即 5%）；无数据返回 None。
        """
        rets = self.get_benchmark_returns(end_date, lookback_days=window + 5)
        if rets is None or rets.empty:
            return None
        recent = rets.iloc[-window:]
        return float((1 + recent).prod() - 1)

    # ── 内部 ──

    def prev_trading_day(self, date_str: str) -> str | None:
        """date_str 的前一交易日（日历查 30 天窗口，找不到返回 None）。

        引擎在 preload 钳制与首日播种时调用（本类内部也复用）；
        缓存按 date_str 记忆，回测中重复查询不重复走日历。
        """
        if date_str in self._prev_day_cache:
            return self._prev_day_cache[date_str]
        lookback = (date.fromisoformat(date_str) - timedelta(days=30)).strftime("%Y%m%d")
        calendar = self.get_calendar(lookback, date_str)
        before = [d for d in calendar if d < date_str]
        prev = before[-1] if before else None
        self._prev_day_cache[date_str] = prev
        return prev
