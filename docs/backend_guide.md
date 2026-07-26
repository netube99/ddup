# 数据后端对接指南

将 ddup 连接到自有 A 股数据库。若数据库为 **SQLite**，
只需填写一份 Python dict 声明每份数据的物理位置（`表名.字段名`），
一行 Python 代码，零 SQL。

---

## 核心理念

ddup 内部消费 `(trade_date, symbol)` 双索引面板，但对接者无需了解内部格式。
唯一需回答的问题：**每份数据在库中属于哪张表的哪个字段？**

数据可分散在任意多张表中——OHLC 分存四张表直接各填各的。
ddup 内部自动按 `(交易日, 代码)` 外并集拼成面板，不存在"主表"概念。

---

## 三步接入

**第 1 步**：在 `adapters/` 下新建文件（如 `adapters/my_db.py`），
复制模板，逐空填入库中真实位置。

**第 2 步**：运行校验。表名/列名拼写错误、必需项缺失、表单结构非法，
均在初始化时直接报错并精确指出条目：

```bash
python -c "from adapters.my_db import MyBackend; MyBackend('/path/to/your.db')"
```

**第 3 步**：在 CLI 或 Python 脚本中使用该 backend：

```bash
python scripts/run.py my_strategy.yaml --start 20240101 --end 20240630
```
（行情数据库路径由 adapter 内的 `_DEFAULT_DB_PATH` 决定，无需在命令行指定）

---

## 填表模板

```python
from btcore.generic_sql import GenericSQLBackend

FORM = {
    # ════════════════════════════════════
    # 通用查询键列名（各表共用，只写列名不带表名）
    # ════════════════════════════════════
    "symbol": "",               # 证券代码列名，如 "ts_code"
    "date": "",                 # 交易日列名，YYYYMMDD 字符串，如 "trade_date"

    # ════════════════════════════════════
    # 引擎必需 — 14 个位置空（都填 "表名.字段名"）
    # ════════════════════════════════════

    # —— 数据契约 10 字段（口径见下方契约表）——
    "open": "",                 # 开盘价（裸价，元），如 "daily.open"
    "high": "",                 # 最高价
    "low": "",                  # 最低价
    "close": "",                # 收盘价
    "vol": "",                  # 成交量（手，1 手 = 100 股）
    "amount": "",               # 成交额
    "adj_factor": "",           # 复权因子（不可缺/NaN）
    "pre_close": "",            # 昨收价（交易所除权调整口径）
    "up_limit": "",             # 涨停价，如 "stk_limit.up_limit"
    "down_limit": "",           # 跌停价

    # —— 交易日历与分红 ——
    "calendar_date": "",        # 日历表的日期列，如 "trade_cal.cal_date"
    "dividend_ex_date": "",     # 除权除息日，如 "dividend.ex_date"
    "dividend_stk_div": "",     # 每股送转（与除权日同一张表）
    "dividend_cash_div": "",    # 每股现金红利（与除权日同一张表）

    # ════════════════════════════════════
    # 引擎辅助能力 — 7 个能力空（不填 = 该能力关闭）
    # ════════════════════════════════════
    # "st_symbol": "",           # ST 标记表的代码列
    # "industry_name": "",       # 行业分类列
    # "listing_date": "",        # 上市日期列
    # "index_code": "",          # 指数代码列（与 index_member 成对）
    # "index_member": "",        # 指数成分列（同一张表）
    # "benchmark_close": "",     # 基准收盘价
    # "benchmark_adj_factor": "",  # 基准复权因子（可选；指数点位基准不填）
    "benchmark_code": "000300.SH",  # 默认基准代码（填了 benchmark_close 就建议填）

    # ════════════════════════════════════
    # 自选扩展字段（策略/因子要用什么加什么）
    # ════════════════════════════════════
    # "extra_fields": {
    #     "turnover_rate": "daily.turnover_rate",
    #     "pe_ttm": "daily.pe_ttm",              # exclude_loss 过滤规则依赖
    #     "total_mv": "daily.total_mv",          # log_mktcap 因子伪列依赖
    # },

    # ════════════════════════════════════
    # 沉底：表的特殊说明（多数库为空即可）
    # ════════════════════════════════════
    # "tables": {
    #     "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
    #     "dividend": {"filter": {"div_proc": "实施", "ex_date": None}},
    #     "某张表": {"symbol": "code", "date": "dt"},    # 键列名与全局不同
    # },
}


class MyBackend(GenericSQLBackend):
    def __init__(self, db_path: str):
        super().__init__(FORM, db_path)
```

填好的完整示例见 `adapters/tushare.py`。

---

## 数据契约：10 个必需字段的口径

| 字段 | 单位 / 口径 | 说明 |
|---|---|---|
| `open` | 裸价，元 | 当日开盘价 |
| `high` | 裸价，元 | 当日最高价 |
| `low` | 裸价，元 | 当日最低价 |
| `close` | 裸价，元 | 当日收盘价 |
| `vol` | **手**（1手=100股） | 成交量。`order_volume_ratio` 按此单位解释 |
| `amount` | 元 | 成交额 |
| `adj_factor` | 无量纲 | 复权因子。**不可缺/NaN**：引擎据此精确派生 `*_hfq` |
| `pre_close` | 元 | 昨收价，**交易所除权调整口径**：除权日 = (前裸收盘 - 现金分红) / (1 + 送转比例) |
| `up_limit` | 元 | 涨停价。允许个别行 NaN（表示当日涨跌停不可判，相关订单跳过） |
| `down_limit` | 元 | 跌停价。同允许个别行 NaN |

关键说明：

- **不要填 `*_hfq` 和 `pct_chg`**——引擎从 `adj_factor` / `pre_close` 精确派生，
  填了会被表单拒绝。
- **日期列统一用 YYYYMMDD 字符串**。`query_bars` 返回的 DataFrame 索引固定为
  `(trade_date, symbol)`，与库中物理列名无关。
- **这 10 列是硬契约**：缺任何一列引擎直接报错
  `ValueError: bars 缺必需列: [...]`。引擎不做语义有损耗的兜底推算。

---

## 每个能力空位的成本与行为

### `st_symbol` — ST 标记

**数据形态**：一张日频快照表，每天一行记录一个 ST 股。
当日有记录 = 当日 ST；摘帽次日自动恢复可买（次日不再有该股的记录）。

**数据准备成本**：若使用 tushare 的 `namechange` 表，需处理成日频快照。
实际上是一张"今天哪些股票是 ST"的表。

**解锁能力**：`StockFilter` 的 `exclude_st` 过滤规则。

**缺失行为**：软回退——`exclude_st` 开启时告警一次
`"exclude_st 已开启但 backend 未提供 get_st_map，ST 过滤不生效"`，
策略继续运行但不排 ST。

### `industry_name` — 行业分类

**数据形态**：一张代码到行业名称的映射表。可以是截面快照（如申万行业分类表），
只需要能按代码列表返回 `{symbol: industry_name}`。

**数据准备成本**：较低。一张静态或定期更新的映射表即可。

**解锁能力**：`StockFilter` 的 `exclude_industries` 行业过滤；因子 `group_mean` /
`group_rank` / `neutralize` 算子的行业分组；`industry` 伪列；`risk_rules.max_industry_pct` 行业上限。

**缺失行为**：行业过滤告警后软回退；因子/风控引用 `industry` 时 **preload 直接报错**
`"因子引用 industry 分组需要 backend 提供 get_stock_industries"`。

### `listing_date` — 上市日期

**数据形态**：一张代码到上市日期的映射表。

**数据准备成本**：很低。静态映射表即可。

**解锁能力**：`StockFilter` 的 `exclude_new_stock` 新股过滤。

**缺失行为**：软回退——告警一次 `"exclude_new_stock 已开启但 backend 未提供 get_recent_listings，次新股过滤不生效"`。

### `index_code` + `index_member` — 指数成分（成对必填）

**数据形态**：指数成分月频快照表，每行记录 (指数代码, 成分代码)。
系统取多指数并集，取最近一期 ≤ 当日的快照过滤。

**数据准备成本**：中。需要维护指数成分的历史快照数据（月频即可）。

**解锁能力**：`filter_rules.index_universe` 指数成分白名单；loader 自动生成
`get_universe` 用于 preload 数据裁剪。

**缺失行为**：软回退——告警一次 `"index_universe 已开启但 backend 未提供 get_index_members，白名单规则不生效"`。

**注意**：index_code 和 index_member 必须填在同一张表里，且两项必须成对（填一个不填另一个在表单校验时报错）。

### `benchmark_close` — 基准收盘价

**数据形态**：一张 (代码, 日期) 对应的收盘价表。可以是 ETF（如 510300）或指数点位。

**数据准备成本**：低。一张日频价格表即可。

**解锁能力**：回测统计中的基准对比（年化收益、超额收益、信息比率等）；
因子伪列 `idx_ret`（需要 benchmark_close + 策略 config 中的 `benchmark` 代码非空）。

**缺失行为**：基准为 None（统计输出不含基准对比列，但不报错）；
`idx_ret` 被引用时 preload 直接报错
`"因子引用 idx_ret 需要 config['benchmark'] 且 backend 提供 get_benchmark_bars"`。

### `benchmark_adj_factor` — 基准复权因子（可选）

**数据形态**：与 `benchmark_close` 配套的复权因子表。仅当基准是 ETF 等需要复权
的品种时才需要填。

**数据准备成本**：如果基准是指数点位（如 000300.SH），不填即可——
系统直接用原始 close 作为 hfq_close。

**解锁能力**：基准 hfq_close 的精确计算（区间首日锚定）。

**缺失行为**：退化为未复权 close（等价于 `hfq_close = close`），不报错。

---

## 自选扩展字段（extra_fields）

契约 10 字段之外，因子/策略所需的日频数据在 `extra_fields` 中逐列声明。
格式与所有其他位置空一样：`"列名": "表名.字段名"`。

```python
"extra_fields": {
    "turnover_rate": "daily.turnover_rate",   # 换手率
    "pe_ttm": "daily.pe_ttm",                 # 市盈率 TTM（exclude_loss 依赖）
    "total_mv": "daily.total_mv",             # 总市值（log_mktcap 伪列依赖）
    "circ_mv": "daily.circ_mv",               # 流通市值
    "pb": "daily.pb",                         # 市净率
}
```

- 声明了的列才能被 `query_bars` 返回（引擎按策略声明自动列裁剪，没请求的表整表不查）
- 没声明的列查询时报错 `ValueError: query_bars 未知列名: ['xxx']（未在表单中声明）`
- 要加新数据，回来加一行即可，不需要改任何引擎代码

以下列名是**保留名**，不能作为 extra_fields 的键：`open_hfq` `high_hfq`
`low_hfq` `close_hfq` `pct_chg`（引擎派生列）、`idx_ret` `log_mktcap`
`industry`（因子伪列）、`symbol` `trade_date`（索引键）。

物理表/列名撞 SQL 保留字（如某库的 `limit` 列）照常填 `表名.字段名` 即可——
生成的 SQL 对所有标识符统一加双引号包裹。

---

## tables 节：表的特殊说明

以下手段只在常规填法表达不了时才用。大多数库不需要。
它们都住在 `tables` 节里——**某张表有特殊之处，就在 `tables` 里给它加一条**。

### filter：筛选有效行

只对**日历 / 分红 / ST / 指数成分**四类角色表有效。
行情网格的行集是"库里有就有"，不配 filter。

```python
"tables": {
    "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
    "dividend": {"filter": {"div_proc": "实施", "ex_date": None}},  # None → IS NOT NULL
}
```

filter 的值要与库里的存储类型一致（`is_open` 存整数就写 `1`，存文本就写 `"1"`）。

### symbol / date：键列名覆盖

某张表的代码列或日期列名字与其他表不同时，在同一条里指出。
这对**任何被引用的表**都有效。

```python
"tables": {"stk_limit": {"symbol": "code", "date": "dt"}}
```

### filter_sql：逃生舱

filter 名值对表达不了的筛选条件（如 `CAST(...)`、区间判断），
写原始 SQL 片段（AND 拼接）。尽量不用：

```python
"tables": {"trade_cal": {"filter_sql": "CAST(is_open AS INTEGER) = 1"}}
```

---

## 非 SQLite 数据源：手写 DataBackend

如果数据不在 SQLite 里（CSV、API、其他数据库），直接实现 `DataBackend` ABC：

```python
from btcore.backend import DataBackend

class MyBackend(DataBackend):
    def query_bars(self, symbols, start, end, columns=None):
        """返回 MultiIndex (trade_date, symbol) 的 DataFrame。
        columns 参数做列裁剪，未知列名必须 ValueError。"""
        ...

    def get_calendar(self, start, end):
        """返回 YYYYMMDD 字符串列表。"""
        ...

    def get_dividends_on_date(self, date_str):
        """返回 {symbol: {stk_div, cash_div}}。"""
        ...
```

10 个契约列的要求同上。库中其他数据以方法形式添加至后端类，
（如 `get_stock_industries`），策略经 `provider.backend.xxx()` 鸭子类型调用。

参考 `tests/test_foreign_backend.py`（约 270 行，对接一个完全不同的假数据库）。

### 子类化 GenericSQLBackend

填表表达不了的个别逻辑（如特殊的分红计算），继承 `GenericSQLBackend`
覆盖单个方法即可，其余行为保持表单驱动：

```python
class MyBackend(GenericSQLBackend):
    def get_dividends_on_date(self, date_str):
        ...  # 自定义分红逻辑
```

---

## 常见问题

**Q: 初始化报 "表单引用的列在库中不存在"？**

报错会指出是哪个表单条目（如 `'pe_ttm' → 表.列`），对照修改拼写即可。
校验在初始化时完成，所有拼写错误当场暴露，不会拖到回测中途。

**Q: 同一只票同一天在某些表有数据、某些表没有？**

正常。缺失的字段是 NaN：价格为 NaN 的行撮合层自动跳过（`is_valid_price`），
扩展字段为 NaN 由因子/策略按 pandas 惯例处理。

**Q: 财报数据怎么处理？**

ddup 只消费 `(交易日, 代码)` 日频网格上的列。
财报类数据须在数据层按**公告日**（而非报告期）对齐成日频列
（建 VIEW 或物化表），跨季度运算（YoY 等）也预先算成列。
引擎不做季度频率推断。

```sql
CREATE VIEW v_financials AS
SELECT ts_code, ann_date AS trade_date, ... FROM financials;
```

然后在表单里引用 VIEW：`"pe_ttm": "v_financials.pe_ttm"`。
