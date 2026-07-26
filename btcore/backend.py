"""数据后端契约 — 项目对数据库的全部需求。

项目只定义这 3 个抽象方法。你的数据库里若还有别的数据
（ST、行业、基准、自有特色字段等），在自己的后端类上自行添加方法，
消费方鸭子类型调用，项目不预定义这些方法的名称与签名。

用户后端实现放在顶层 adapters/ 目录（如 adapters/tushare.py）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd


class DataBackend(ABC):
    """数据后端契约。"""

    @abstractmethod
    def query_bars(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """[start, end] 闭区间内的 K 线数据。symbols=None 表示全部股票。

        columns=None 表示全部列；传入列名列表时只返回这些列（列裁剪，
        引擎 preload 按策略声明静态推导所需列，宽表场景下大幅降低内存）。
        实现方对不存在的列名应快速报错，不得静默忽略。

        返回 DataFrame，MultiIndex (trade_date, symbol)，日期为 YYYYMMDD 字符串。

        必需列（缺列引擎 preload 直接报错，语义见 docs/backend_guide.md）：
          open / high / low / close  裸价（元）
          vol                        成交量，单位手（1 手 = 100 股）
          amount                     成交额
          adj_factor                 复权因子
          pre_close                  昨收，交易所除权调整口径：
                                     除权日 = (前裸收盘 - 现金分红) / (1 + 送转比例)
          up_limit / down_limit      精确涨跌停价（元）
        *_hfq / pct_chg 由引擎精确派生，无需提供。
        任何日线扩展数据（moneyflow、情绪评分等）作为额外列直接加入即可，
        前视防护对所有列自动生效。
        """
        ...

    @abstractmethod
    def get_calendar(self, start: str, end: str) -> list[str]:
        """交易日历列表（YYYYMMDD 格式）。"""
        ...

    @abstractmethod
    def get_dividends_on_date(self, date_str: str) -> dict[str, dict[str, float]]:
        """当日除权除息 {symbol: {stk_div, cash_div}}。"""
        ...
