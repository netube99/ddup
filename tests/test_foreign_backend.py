"""验证 DataBackend ABC 通用性：用一个和 tushare_db 完全不同的假数据库。

- 数据源：纯内存 list[dict], 非 SQLite
- 股票代码：CUSTOM_001 ~ CUSTOM_010, 非 tushare 格式
- 日历：虚构的连续自然日, 非 SSE 交易日历
- 提供契约要求的全部必需列（含 pre_close 除权口径 / up_limit / down_limit,
  由后端自己按规则计算）
- 没有辅助表、没有股票信息
- 外加一个虚构的情绪指标(sentiment_score), 来自完全不同的数据源
"""

import pandas as pd

from btcore.backend import DataBackend
from btcore.engine import Engine
from btcore.provider import DataProvider


class ForeignBackend(DataBackend):
    """别人的数据库 — 结构和你完全不同。

    - 数据是内存里的原始 list[dict], 不经过 SQL
    - 价格单位是分(100 分 = 1 元), query_bars 里转换成元
    - symbol 格式是 CUSTOM_NNN, 不是 tushare 的 XXXXXX.SH
    - 日历是虚构的连续自然日, 不分交易日/非交易日
    - 分红数据独立维护, 不来自 dividend 表
    - pre_close / up_limit / down_limit 自己按规则计算（契约必需列,
      除权日 pre_close 按交易所口径调整）
    - 没有 ST、没有行业、没有成分股
    - 没有 get_benchmark_bars / get_all_components / get_stock_industries

    别人有自己的特色数据：社交媒体情绪评分。
    存于独立的 CSV 文件(此处模拟为内存 dict), 在 query_bars 里 join。
    """

    def __init__(self):
        # 手工维护的分红记录 — 不来自 dividend 表
        self._dividends = {
            "20240103": {"CUSTOM_001": {"stk_div": 0.0, "cash_div": 0.05}},
            "20240107": {"CUSTOM_005": {"stk_div": 0.3, "cash_div": 0.0}},
        }

        # 原始行情数据: 每行是一个 list, 包含价格单位: 分
        self._raw_rows = []
        self._build_fake_data()

        # 别人的特色数据：社交媒体情绪评分 (0~100)
        # 来自完全不同的数据管道, 和行情表毫不相关
        # 格式: {(date, symbol): score}
        self._sentiment = self._build_sentiment()

    def _build_fake_data(self):
        """生成 10 只股票 x 10 天的假数据, 价格单位: 分."""
        prev_close = {}  # symbol -> 前一日裸收盘 (元)
        for day in range(10):
            date_str = f"202401{(day + 1):02d}"
            divs = self._dividends.get(date_str, {})
            for sid in range(1, 11):
                sym = f"CUSTOM_{sid:03d}"
                base_price = 1000 + sid * 50  # 分
                close = (base_price + day * 10 + 15) / 100.0  # 元
                pc = prev_close.get(sym)
                if pc is not None:
                    div = divs.get(sym)
                    if div:
                        # 交易所除权口径: (前裸收盘 - 现金分红) / (1 + 送转比例)
                        pc = (pc - div["cash_div"]) / (1.0 + div["stk_div"])
                    up = round(pc * 1.1, 2)
                    down = round(pc * 0.9, 2)
                else:
                    up = down = None  # 上市首日无前收, 涨跌停不可判
                self._raw_rows.append([
                    sym,
                    date_str,
                    base_price + day * 10,        # open (分)
                    base_price + day * 10 + 30,   # high
                    base_price + day * 10 - 5,    # low
                    base_price + day * 10 + 15,   # close
                    1000000,                       # vol (手)
                    10000000,                      # amount
                    1.0,                           # adj_factor (无复权)
                    pc,                            # pre_close (元, 除权调整口径)
                    up,                            # up_limit (元)
                    down,                          # down_limit (元)
                ])
                prev_close[sym] = close

    def _build_sentiment(self) -> dict:
        """别人的情感分析团队产出的情绪数据。

        和你的 moneyflow/cyq_perf 一样是第三方数据源，
        但在 query_bars 里 join 对上层完全透明。
        """
        scores = {}
        for day in range(10):
            date_str = f"202401{(day + 1):02d}"
            for sid in range(1, 11):
                sym = f"CUSTOM_{sid:03d}"
                # 编号大的股票情绪更高(模拟真实数据特征)
                scores[(date_str, sym)] = 30.0 + sid * 7.0 + day * 0.5
        return scores

    def query_bars(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """把原始 list[list] 转成 DataFrame, 分转元, join 情绪数据。

        别人的同事写的查询逻辑, 和你的 SQL 毫不相关。
        情绪数据是独立的 dict key join, 不是 SQL JOIN。
        columns=None 全列; 传列名列表时只返回这些列 (列裁剪契约)。
        """
        rows = []
        for row in self._raw_rows:
            sym, date_str = row[0], row[1]
            if symbols and sym not in symbols:
                continue
            if date_str < start or date_str > end:
                continue
            key = (date_str, sym)
            rows.append({
                "symbol": sym,
                "trade_date": date_str,
                "open": row[2] / 100.0,
                "high": row[3] / 100.0,
                "low": row[4] / 100.0,
                "close": row[5] / 100.0,
                "vol": row[6],
                "amount": row[7] / 100.0,
                "adj_factor": row[8],
                "pre_close": row[9],
                "up_limit": row[10],
                "down_limit": row[11],
                # 别人的特色字段, 直接加进 bars_df
                "sentiment_score": self._sentiment.get(key, 50.0),
            })
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df.set_index(["trade_date", "symbol"], inplace=True)
        if columns is not None:
            unknown = set(columns) - set(df.columns)
            if unknown:
                raise ValueError(f"query_bars 未知列名: {sorted(unknown)}")
            df = df.loc[:, sorted(set(columns))]
        return df

    def get_calendar(self, start: str, end: str) -> list[str]:
        """虚构的连续自然日 — 不区分交易日和非交易日。

        别人的日历系统, 和你的 trade_cal 表完全无关。
        """
        return [f"202401{i:02d}" for i in range(1, 11)
                if f"202401{i:02d}" >= start and f"202401{i:02d}" <= end]

    def get_dividends_on_date(self, date_str: str) -> dict:
        """手工维护的分红记录 — 不来自 dividend 表。

        别人的分红数据格式。
        """
        return self._dividends.get(date_str, {})

    # ── 非日线数据 (鸭子类型, 不需要前视防护) ──

    def get_internal_rating(self, symbol: str) -> str:
        """内部股票评级。研究员手工打分, 非日线数据。

        和你的 get_stock_industries 一样：
        - 静态/事件驱动数据, 不存在前视偏差
        - 通过 backend 鸭子类型暴露, 策略通过 provider.backend 调用
        """
        ratings = {f"CUSTOM_{sid:03d}": "A" if sid <= 3
                   else "B" if sid <= 7 else "C"
                   for sid in range(1, 11)}
        return ratings.get(symbol, "N/A")


# ── 用情绪因子选股的策略 ──

class SentimentStrategy:
    """别人的策略：每天买情绪评分最高的一只股票。

    策略不需要知道 sentiment_score 来自 SQL JOIN 还是 dict lookup，
    和 TushareBackend 的 moneyflow/cyq_perf/margin_detail 一样：
    bar 里有的列就能用。
    select() 里命令式访问的列声明进 REQUIRED_FIELDS（列裁剪契约）。
    """

    REQUIRED_FIELDS = ["sentiment_score"]

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 3, "top_k": 30}

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def on_start(self, provider, first_date, end_date=None):
        pass

    def select(self, bars, snapshot, provider):
        """买情绪评分最高的一只股票(排除已有持仓)。"""
        current = set(snapshot.holdings.keys())
        candidates = {s: b for s, b in bars.items() if s not in current}
        if not candidates:
            return {"buy": [], "sell": []}
        best = max(candidates, key=lambda s: candidates[s].get("sentiment_score", 0))
        return {"buy": [best], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_foreign_backend_runs():
    """端到端: 用完全不同的数据库跑回测。"""
    backend = ForeignBackend()
    provider = DataProvider(backend)
    strategy = SentimentStrategy()

    engine = Engine(strategy, provider, initial_capital=1_000_000,
                    db_path=":memory:", max_positions=3)
    result = engine.run("20240101", "20240110")

    stats = result["statistics"]
    assert "total_return" in stats
    assert stats["total_days"] >= 1
    assert result["account_daily"] is not None
    assert result["trade_log"] is not None


def test_sentiment_field_accessible_in_bars():
    """别人的特色字段在 bars dict 中可用, 策略无需知道数据来源。"""
    backend = ForeignBackend()
    provider = DataProvider(backend)
    bars_df = provider.get_engine_bars(None, "20240105")

    # 契约必需字段 + sentiment_score 都有
    assert "open" in bars_df.columns
    assert "close" in bars_df.columns
    assert "sentiment_score" in bars_df.columns

    # CUSTOM_010 情绪最高(=30+10*7+5*0.5=102.5)
    row = bars_df.loc[pd.IndexSlice["20240105", "CUSTOM_010"]]
    assert row["sentiment_score"] > 90


def test_historical_bars_excludes_today_aux_fields():
    """get_historical_bars 不含当日, 辅助字段(sentiment_score)同样被保护。

    策略调 get_historical_bars("20240105") 只能看到 20240104 及之前的数据。
    前视防护对所有列生效, 不关 DataProvider 知不知道 sentiment_score 是什么。
    """
    backend = ForeignBackend()
    provider = DataProvider(backend)

    # 策略拿历史 bars — 不含 20240105
    hist = provider.get_historical_bars(["CUSTOM_001"], "20240105", lookback_days=30)
    dates = hist.index.get_level_values("trade_date").unique()

    assert "20240105" not in dates, "历史 bars 不应该包含当日"
    assert "20240104" in dates, "应该包含前一日的辅助字段数据"
    assert "sentiment_score" in hist.columns


def test_non_daily_data_no_protection_needed():
    """非日线数据不需要前视防护, 通过 provider.backend 直接访问。

    内部评级是静态数据, 不存在"偷看明天的评级"这个问题。
    和你的 get_stock_industries、get_st_symbols、get_st_map 一样,
    策略调 provider.backend.get_internal_rating() 不需要经过 DataProvider。
    """
    backend = ForeignBackend()
    provider = DataProvider(backend)

    # 非日线数据直接从 backend 获取
    assert provider.backend.get_internal_rating("CUSTOM_001") == "A"
    assert provider.backend.get_internal_rating("CUSTOM_005") == "B"
    assert provider.backend.get_internal_rating("CUSTOM_010") == "C"
    assert provider.backend.get_internal_rating("NONEXISTENT") == "N/A"
