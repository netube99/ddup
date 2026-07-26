import logging
from bisect import bisect_right
from datetime import date, timedelta

from btcore.types import bar_get

logger = logging.getLogger(__name__)

# index_universe 成分快照是月频的，加载时向前多取一段，
# 保证回测首日也有 ≤ 当日的快照可用
_INDEX_LOOKBACK_DAYS = 45


def filter_required_columns(rules: dict) -> set[str]:
    """过滤规则对 bars 列的固定依赖（引擎 preload 列裁剪用）。

    只统计显式开启的规则: StockFilter 运行时 exclude_loss 缺省为 True,
    但不用 StockFilter 的策略不应被强制要求 pe_ttm 列; 依赖缺省值的
    策略由 StockFilter 在列缺失时告警提示显式声明。
    """
    if rules.get("exclude_loss"):
        return {"pe_ttm"}
    return set()


class StockFilter:
    """One-time preload of ST list + recent listings, O(n) in-memory filtering.

    backend 参数是数据库后端对象，按规则开启情况需要实现对应方法（鸭子类型）：
      exclude_st         → get_st_map(from_date) -> {date: {symbol, ...}}
                           （ST 表是日频快照：当日有记录 = 当日 ST）
      exclude_new_stock  → get_recent_listings
      exclude_industries → get_stock_industries
      index_universe     → get_index_members(index_codes, start, end)
                           -> {snapshot_date: {symbol, ...}}（多指数并集）
    这些方法不属于 DataBackend ABC，由用户在自己的后端类上自行定义。
    规则开启而方法缺失时不报错：告警一次，该条规则不生效（软回退）。
    """

    def __init__(self, backend, start_date: str, rules: dict,
                 end_date: str | None = None):
        self._backend = backend
        self._rules = rules
        self._st_map: dict[str, set[str]] = {}
        self._recent_listings: set[str] = set()
        self._industry_map: dict[str, str] | None = None
        self._idx_map: dict[str, set[str]] = {}
        self._idx_dates: list[str] = []
        self._pe_checked = False

        if rules.get("exclude_st"):
            if hasattr(backend, "get_st_map"):
                self._st_map = backend.get_st_map(start_date)
            else:
                logger.warning(
                    "exclude_st 已开启但 backend 未提供 get_st_map，"
                    "ST 过滤不生效"
                )

        if rules.get("exclude_new_stock"):
            if hasattr(backend, "get_recent_listings"):
                self._recent_listings = backend.get_recent_listings(
                    cutoff_days=60, as_of=end_date or start_date
                )
            else:
                logger.warning(
                    "exclude_new_stock 已开启但 backend 未提供 get_recent_listings，"
                    "次新股过滤不生效"
                )

        if rules.get("index_universe"):
            if hasattr(backend, "get_index_members"):
                lookback = (
                    date.fromisoformat(start_date)
                    - timedelta(days=_INDEX_LOOKBACK_DAYS)
                ).strftime("%Y%m%d")
                self._idx_map = backend.get_index_members(
                    list(rules["index_universe"]), lookback, end_date or start_date
                )
                self._idx_dates = sorted(self._idx_map)
                if not self._idx_map:
                    logger.warning(
                        "index_universe=%s 无成分数据，白名单规则不生效",
                        rules["index_universe"],
                    )
            else:
                logger.warning(
                    "index_universe 已开启但 backend 未提供 get_index_members，"
                    "白名单规则不生效"
                )

    def _index_members_at(self, date_str: str) -> set[str] | None:
        """date_str 当日的指数成分（最近一期 ≤ 当日的快照；早于首期用首期）。"""
        if not self._idx_dates:
            return None
        i = bisect_right(self._idx_dates, date_str)
        return self._idx_map[self._idx_dates[max(i - 1, 0)]]

    def filter(self, bars: dict, date_str: str) -> dict:
        rules = self._rules
        exclude_boards = set(rules.get("exclude_boards", []))
        min_price = rules.get("min_price", 0.0)
        exclude_st = rules.get("exclude_st", True)
        exclude_new_stock = rules.get("exclude_new_stock", True)
        exclude_loss = rules.get("exclude_loss", True)
        exclude_industries = set(rules.get("exclude_industries", []))
        index_members = self._index_members_at(date_str)

        # ST 按当日快照判定：当日有记录才是 ST，摘帽次日自动恢复可买
        st_set = self._st_map.get(date_str, set())

        # 懒加载行业映射
        if exclude_industries:
            if self._industry_map is None:
                if hasattr(self._backend, "get_stock_industries"):
                    self._industry_map = self._backend.get_stock_industries(
                        list(bars.keys())
                    )
                else:
                    logger.warning(
                        "exclude_industries 已开启但 backend 未提供 "
                        "get_stock_industries，行业过滤不生效"
                    )
                    self._industry_map = {}

        filtered = {}
        for symbol, bar in bars.items():
            if exclude_st and symbol in st_set:
                continue

            if exclude_new_stock and symbol in self._recent_listings:
                continue

            if exclude_boards:
                board = _get_board(symbol)
                if board in exclude_boards:
                    continue

            if exclude_industries:
                industry = self._industry_map.get(symbol) if self._industry_map else None
                if industry in exclude_industries:
                    continue

            # 指数成分白名单：只管入场过滤，持仓被调出指数不强制卖出
            if index_members is not None and symbol not in index_members:
                continue

            close_val = bar_get(bar, "close", 0.0)
            if min_price > 0 and close_val < min_price:
                continue

            if exclude_loss:
                pe = bar_get(bar, "pe_ttm")
                if not self._pe_checked:
                    # exclude_loss 依赖 pe_ttm; 列裁剪下未显式声明
                    # exclude_loss: true 不会 preload 该列, 告警一次
                    self._pe_checked = True
                    if pe is None:
                        logger.warning(
                            "exclude_loss 生效但 bars 无 pe_ttm 列，亏损过滤不生效；"
                            "请在 filter_rules 显式声明 exclude_loss: true 以 preload 该列"
                        )
                if pe is not None and pe <= 0:
                    continue

            filtered[symbol] = bar

        return filtered


def _get_board(symbol: str) -> str:
    if symbol.endswith(".BJ"):
        return "BJ"
    code = symbol.split(".")[0] if "." in symbol else symbol
    if code.startswith("688"):
        return "688"
    if code.startswith("300"):
        return "300"
    if code.startswith("301"):
        return "301"
    return "MAIN"
