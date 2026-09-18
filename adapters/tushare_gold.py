"""黄金研究后端 — 消费 data/gold_market.duckdb（scripts/refresh_gold_db.py 生成）。

与 adapters/tushare.py 的关系：同一套填表法机制，映射到黄金专用库的
gold_panel 表（ETF 行情 + 份额 + 宏观对齐列）。用于单标的/多标的黄金
择时策略；股票策略继续使用 adapters/tushare.py。

CLI 选择：DDUP_BACKEND=adapters.tushare_gold:TushareGoldBackend
（缺省仍为 adapters.tushare:TushareBackend，见 research/cli_common.py）。

数据契约（详见 docs/backend_guide.md）：
  - gold_panel 的宏观列（us_real_y10 / usdcnh / xau_usd / sge_close / m*_yoy）
    由 scripts/refresh_gold_db.py 按"严格早于中国交易日"对齐，无前视；
  - up_limit / down_limit 为 pre_close ± 10% 的合成 ETF 涨跌停价；
  - 黄金 ETF 无分红，dividend 为空表（满足填表法必需空）。
"""

import os

from btcore.generic_sql import GenericSQLBackend

_DEFAULT_DB_PATH = os.environ.get(
    "DDUP_GOLD_DB",
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "data", "gold_market.duckdb"),
)


def get_default_db_path() -> str:
    """默认黄金库路径（DDUP_GOLD_DB 环境变量可覆盖）。"""
    return _DEFAULT_DB_PATH


GOLD_FORM = {
    # ══ 引擎必需 ══
    "symbol": "ts_code",
    "date": "trade_date",

    # 数据契约字段
    "open": "gold_panel.open",
    "high": "gold_panel.high",
    "low": "gold_panel.low",
    "close": "gold_panel.close",
    "vol": "gold_panel.vol",
    "adj_factor": "gold_panel.adj_factor",
    "pre_close": "gold_panel.pre_close",
    "up_limit": "gold_panel.up_limit",
    "down_limit": "gold_panel.down_limit",
    "calendar_date": "trade_cal.cal_date",
    "dividend_ex_date": "dividend.ex_date",
    "dividend_stk_div": "dividend.stk_div",
    "dividend_cash_div": "dividend.cash_div",

    # ══ 引擎辅助能力 ══
    "listing_date": "etf_basic.list_date",
    "benchmark_close": "gold_panel.close",   # 基准 = 黄金 ETF 自身（买入持有）
    "benchmark_code": "518880.SH",

    # ══ 自选扩展字段 ══
    "extra_fields": {
        "amount": "gold_panel.amount",
        "fd_share": "gold_panel.fd_share",
        # 外盘与汇率（严格早于对齐）
        "xau_usd": "gold_panel.xau_usd",
        "usdcnh": "gold_panel.usdcnh",
        # 美债实际/名义收益率（FRED 日频，严格早于对齐）
        "us_real_y5": "gold_panel.us_real_y5",
        "us_real_y10": "gold_panel.us_real_y10",
        "us_real_y30": "gold_panel.us_real_y30",
        "us_nom_y10": "gold_panel.us_nom_y10",
        # 上海金现货（元/克）与货币供应
        "sge_close": "gold_panel.sge_close",
        "m1_yoy": "gold_panel.m1_yoy",
        "m2_yoy": "gold_panel.m2_yoy",
    },

    # ══ 表的特殊说明 ══
    "tables": {
        "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
    },
}


class TushareGoldBackend(GenericSQLBackend):
    """黄金研究库后端：通用机械 + 上面的表单。"""

    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        super().__init__(GOLD_FORM, db_path)
