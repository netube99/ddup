"""Pytest fixtures: MockDataBackend reads parquet fixtures, implements DataBackend."""

import os

import pandas as pd

from btcore.backend import DataBackend
from btcore.types import Account, Holding

FIXTURES_DIR = os.path.join(os.path.dirname(__file__), "fixtures")

# 镜像 adapters/tushare.py 的 aux_tables：辅助日线表 LEFT JOIN 进 bars
_AUX_TABLES = ["moneyflow", "cyq_perf", "margin_detail"]

# make_bar 的 up_limit/down_limit 缺省按主板 ±10% 推算；显式传 None 表示缺失
_AUTO = object()


def make_holding(symbol="000001.SZ", shares=1000, entry_date="20240531",
                 entry_price=10.0, cost=None, last_price=None, locked=False,
                 **kw) -> Holding:
    """构造真实 Holding：cost/last_price 缺省按 entry_price 推导，kw 覆盖任意字段。

    locked 默认 False（多数单测直接走撮合，不经过买入当日的 T+1 锁定）；
    测锁定点显式传 locked=True。
    """
    if cost is None:
        cost = entry_price * shares
    if last_price is None:
        last_price = entry_price
    return Holding(symbol=symbol, shares=shares, entry_date=entry_date,
                   entry_price=entry_price, cost=cost, last_price=last_price,
                   locked=locked, **kw)


def make_account(cash=100_000.0, initial_capital=None, holdings=None,
                 **kw) -> Account:
    """构造真实 Account：initial_capital 缺省等于 cash，kw 覆盖任意字段。"""
    if initial_capital is None:
        initial_capital = cash
    return Account(cash=cash, initial_capital=initial_capital,
                   holdings=holdings or {}, **kw)


def make_bar(open=10.0, high=None, low=None, close=None, pre_close=None,
             up_limit=_AUTO, down_limit=_AUTO, vol=1_000_000.0,
             date="20240603", **extra) -> dict:
    """构造裸价 bar dict：缺省一字平盘、pre_close=open、主板 ±10% 涨跌停。

    up_limit/down_limit 显式传 None 表示数据缺失（触发板块规则回退）。
    """
    return {
        "open": open,
        "high": open if high is None else high,
        "low": open if low is None else low,
        "close": open if close is None else close,
        "pre_close": open if pre_close is None else pre_close,
        "up_limit": round(open * 1.1, 2) if up_limit is _AUTO else up_limit,
        "down_limit": round(open * 0.9, 2) if down_limit is _AUTO else down_limit,
        "vol": vol,
        "trade_date": date,
        **extra,
    }


class MockDataBackend(DataBackend):
    """从 parquet fixtures 读取数据，实现 DataBackend 接口。"""

    def __init__(self, fixtures_dir: str = FIXTURES_DIR):
        self._dir = fixtures_dir
        self._bars = pd.read_parquet(os.path.join(fixtures_dir, "bars.parquet"))
        self._limits = pd.read_parquet(os.path.join(fixtures_dir, "limits.parquet"))
        self._dividends = pd.read_parquet(os.path.join(fixtures_dir, "dividends.parquet"))
        self._st = pd.read_parquet(os.path.join(fixtures_dir, "st.parquet"))
        self._benchmark = pd.read_parquet(os.path.join(fixtures_dir, "benchmark_bars.parquet"))
        self._trade_cal = pd.read_parquet(os.path.join(fixtures_dir, "trade_cal.parquet"))
        self._aux = {
            t: pd.read_parquet(os.path.join(fixtures_dir, f"{t}.parquet"))
            for t in _AUX_TABLES
        }

        # 预建股利索引（NaN 按 0 处理，or 短路挡不住 float("nan")）
        self._dividend_idx: dict[str, dict] = {}
        for _, r in self._dividends.iterrows():
            stk, cash = r.get("stk_div"), r.get("cash_div")
            self._dividend_idx.setdefault(r["ex_date"], {})[r["ts_code"]] = {
                "stk_div": float(stk) if pd.notna(stk) else 0.0,
                "cash_div": float(cash) if pd.notna(cash) else 0.0,
            }

    # ── 行情 ──

    def query_bars(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        mask = (self._bars["trade_date"] >= start) & (self._bars["trade_date"] <= end)
        if symbols:
            mask &= self._bars["symbol"].isin(symbols)
        result = self._bars.loc[mask].copy()

        # LEFT JOIN 涨跌停 + 辅助日线表（镜像 adapters/tushare.py 的 pandas 级 join）
        aux_frames = [
            self._limits.loc[:, ["trade_date", "ts_code", "up_limit", "down_limit"]],
            *self._aux.values(),
        ]
        for aux in aux_frames:
            aux_mask = (aux["trade_date"] >= start) & (aux["trade_date"] <= end)
            if symbols:
                aux_mask &= aux["ts_code"].isin(symbols)
            aux_df = aux.loc[aux_mask].rename(columns={"ts_code": "symbol"})
            result = result.merge(aux_df, on=["symbol", "trade_date"], how="left")

        result.set_index(["trade_date", "symbol"], inplace=True)
        if columns is not None:
            unknown = set(columns) - set(result.columns)
            if unknown:
                raise ValueError(f"query_bars 未知列名: {sorted(unknown)}")
            result = result.loc[:, sorted(set(columns))]
        return result

    # ── 交易日历 ──

    def get_calendar(self, start: str, end: str) -> list[str]:
        cal = self._trade_cal
        mask = (
            (cal["cal_date"] >= start)
            & (cal["cal_date"] <= end)
            & (cal["is_open"] == 1)
        )
        return sorted(cal.loc[mask, "cal_date"].tolist())

    # ── 除权除息 ──

    def get_dividends_on_date(self, date_str: str) -> dict[str, dict[str, float]]:
        return self._dividend_idx.get(date_str, {})

    # ── ST 标记 ──

    def get_st_map(self, from_date: str) -> dict[str, set[str]]:
        mask = (self._st["trade_date"] >= from_date) & (self._st["type"] == "ST")
        subset = self._st.loc[mask].sort_values("trade_date")
        result: dict[str, set[str]] = {}
        for _, r in subset.iterrows():
            result.setdefault(r["trade_date"], set()).add(r["ts_code"])
        return result

    # ── 股票信息 ──

    def get_stock_industries(self, ts_codes: list[str]) -> dict[str, str]:
        return {}

    def get_recent_listings(
        self, cutoff_days: int = 60, as_of: str | None = None
    ) -> set[str]:
        return set()

    def get_index_members(
        self, index_codes: list[str], start: str, end: str
    ) -> dict[str, set[str]]:
        return {}

    # ── 基准 ──

    def get_benchmark_bars(
        self, code: str = "000300.SH", start: str = "", end: str = ""
    ) -> pd.DataFrame | None:
        if self._benchmark.empty or "hfq_close" not in self._benchmark.columns:
            return None
        bm = self._benchmark
        if start:
            bm = bm[bm["trade_date"] >= start]
        if end:
            bm = bm[bm["trade_date"] <= end]
        bm = bm.copy()
        bm["trade_date"] = pd.to_datetime(bm["trade_date"])
        bm.set_index("trade_date", inplace=True)
        return bm[["hfq_close"]]
