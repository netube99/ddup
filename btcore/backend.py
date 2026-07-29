"""数据后端契约 — 引擎对数据层的全部需求。

============================================================================
两种接入方式（二选一）
============================================================================

A) 填表法（SQLite 数据库，数据在表里）
   子类化 btcore.generic_sql.GenericSQLBackend，只填一个 Python dict 说明
   "表名.字段名" 的位置。零 SQL，全部由通用机械自动生成。
   完整示例见 adapters/tushare.py，教程见 docs/backend_guide.md §2。

B) 手写实现（非 SQL 数据源：内存、API、parquet、CSV 等）
   直接子类化本文件的 DataBackend，实现全部抽象方法，并按需添加能力方法。
   教程见 docs/backend_guide.md §3，完整示例见 tests/conftest.py
   （MockDataBackend）和 tests/test_foreign_backend.py（ForeignBackend）。

============================================================================
方法清单
============================================================================

┌─ 抽象方法（必须实现）────────────────────────────────────────────────────┐
│ query_bars              K 线 + 扩展数据，MultiIndex (trade_date, symbol)  │
│ get_calendar            交易日历列表（YYYYMMDD）                          │
│ get_dividends_on_date   当日除权除息                                      │
└──────────────────────────────────────────────────────────────────────────┘

┌─ 鸭子类型能力方法 ─────────────────────────────────────────────────────────┐
│                                                                              │
│ 引擎调用（不实现 = 对应功能关闭）                                            │
│   get_benchmark_bars      基准行情 → 基准收益统计、idx_ret 因子              │
│   get_st_map              ST 名单日频快照 → exclude_st 过滤                  │
│   get_stock_industries    行业分类 → 行业风控、industry 分组、行业过滤        │
│   get_recent_listings     近期新股 → exclude_new_stock 过滤                  │
│   get_index_members       指数成分 → index_universe / factor_universe        │
│                                                                              │
│ 策略便利方法（引擎不调用，给策略 select() 里用的）                            │
│   get_st_symbols          当日 ST 名单 → 策略按需查询单日 ST                  │
└──────────────────────────────────────────────────────────────────────────────┘

引擎通过 getattr(backend, "方法名", None) 检测能力是否存在，不存在时相关
功能自动降级（跳过对应过滤/风控，或报错提示配置了但后端不支持）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataBackend(ABC):
    """数据后端契约。子类化并实现抽象方法，按需添加能力方法。"""

    # ══════════════════════════════════════════════════════════════════
    # 抽象方法（必须实现）
    # ══════════════════════════════════════════════════════════════════

    @abstractmethod
    def query_bars(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """查询 [start, end] 闭区间内的日线数据。

        Parameters
        ----------
        symbols : list[str] | None
            股票代码列表；None 表示全部股票（引擎 preload 用）。
        start, end : str
            日期区间（含两端），YYYYMMDD 格式。
        columns : list[str] | None
            请求的列名；None 返回全部可用列。实现方对不存在的列名应快速报错，
            不得静默忽略。

        Returns
        -------
        pd.DataFrame
            MultiIndex (trade_date, symbol)，日期为 YYYYMMDD 字符串。

        ── 数据契约列（缺列引擎 preload 直接报错）─────────────────────
        open / high / low / close   裸价（元）
        vol                         成交量，单位：手（1 手 = 100 股）
        adj_factor                  后复权因子（除数法：hfq_close = close / adj_factor）
        pre_close                   昨收价，交易所除权调整口径：
                                    除权日：pre_close = (前裸收盘 - 现金分红) / (1 + 送转比例)
                                    非除权日：pre_close = 前裸收盘
        up_limit / down_limit       精确涨跌停价（元），不可用 ±10% 近似值
        amount                      成交额（元），引擎内部不消费，策略按需通过 REQUIRED_FIELDS 声明

        以下列由引擎精确派生，**勿提供**：
        open_hfq / high_hfq / low_hfq / close_hfq / pct_chg

        ── 扩展列 ─────────────────────────────────────────────────
        日线扩展数据（资金流、情绪评分、筹码分布等）作为额外列直接加入
        DataFrame，前视防护对所有列自动生效，引擎和策略无需知道数据来源。
        """
        ...

    @abstractmethod
    def get_calendar(self, start: str, end: str) -> list[str]:
        """返回 [start, end] 区间内的交易日列表，YYYYMMDD 格式，升序。

        必须包含 start 和 end（如果当天是交易日）。
        """
        ...

    @abstractmethod
    def get_dividends_on_date(self, date_str: str) -> dict[str, dict[str, float]]:
        """返回指定日期的除权除息记录。

        Returns
        -------
        dict[str, dict]
            {symbol: {"stk_div": float, "cash_div": float}}
            stk_div  每股送转比例（10 送 3 → 0.3）
            cash_div 每股现金红利（元）
            无除权除息的日期返回空 dict。
        """
        ...

    # ══════════════════════════════════════════════════════════════════
    # 鸭子类型能力方法（按需实现；不实现 = 该能力关闭）
    #
    # 注意：这些方法 NOT 抽象——引擎通过 getattr(backend, "xxx", None)
    # 检测，不存在时相关功能自动降级。子类按需添加即可，签名必须匹配。
    # ══════════════════════════════════════════════════════════════════

    # 以下为接口文档，不是可执行代码。签名和语义供实现参考。
    #
    # def get_benchmark_bars(
    #     self, code: str = "000300.SH", start: str = "", end: str = ""
    # ) -> pd.DataFrame | None:
    #     """基准指数日线数据。
    #
    #     Returns
    #     -------
    #     pd.DataFrame | None
    #         index: trade_date (YYYYMMDD 字符串)
    #         columns: ["hfq_close"]（后复权收盘价）
    #         无数据返回 None。
    #     """
    #     ...
    #
    # def get_st_symbols(self, trade_date: str) -> set[str]:
    #     """返回当日处于 ST 状态的股票代码集合（策略便利方法，引擎不调用）。"""
    #     ...
    #
    # def get_st_map(self, from_date: str) -> dict[str, set[str]]:
    #     """返回从 from_date 起每日的 ST 名单。
    #
    #     Returns
    #     -------
    #     dict[str, set[str]]
    #         {date: {symbol, ...}}，date 为 YYYYMMDD 字符串。
    #     """
    #     ...
    #
    # def get_stock_industries(self, ts_codes: list[str]) -> dict[str, str]:
    #     """返回股票代码到行业名称的映射。
    #
    #     Returns
    #     -------
    #     dict[str, str]
    #         {symbol: 行业名称}。未找到的股票不出现在结果中。
    #     """
    #     ...
    #
    # def get_recent_listings(
    #     self, cutoff_days: int = 60, as_of: str | None = None
    # ) -> set[str]:
    #     """返回近期上市的股票代码集合（上市日距 as_of ≤ cutoff_days）。
    #
    #     as_of 为 None 时取当前日期。
    #     """
    #     ...
    #
    # def get_index_members(
    #     self, index_codes: list[str], start: str, end: str
    # ) -> dict[str, set[str]]:
    #     """返回指定指数在 [start, end] 区间内每日的成分股。
    #
    #     Returns
    #     -------
    #     dict[str, set[str]]
    #         {date: {symbol, ...}}，date 为 YYYYMMDD 字符串。
    #     """
    #     ...
