# 数据库对接指南

ddup 引擎通过 `DataBackend` 抽象层消费数据，用户有两种接入方式：

| 方式 | 适用场景 | 工作量 |
|------|---------|--------|
| **A) 填表法** | 数据在 SQLite 数据库里（如 tushare 本地库） | 填一个 Python dict，零 SQL |
| **B) 手写实现** | 非 SQLite 数据源（内存、API、parquet、CSV 等） | 实现 3 个抽象方法 |

推荐填表法。本文 §2 和 §4–§8 详细说明填表法，§3 说明手写实现。

---

## 1. 快速开始

本项目推荐搭配数据库管理软件 [tushare_db](https://github.com/netube99/tushare_db) 一起使用，开发过程已原生适配基于该工具创建维护的数据库，理论上适配环节只需修改 SQLite 库路径即可完成适配，且引擎内部对某些数据的依赖基于 tushare.pro 原生的表组织形式，匹配第三方数据库可能会出现预期之外的问题

**注意：tushare.pro 积分等级需要满足 `2000` 才能获取到满足引擎运行的必须数据要求，其中 stock_st 接口要求账户满足积分等级 `3000`，缺少 stock_st 表引擎无法正确筛选ST股**

```bash
# 1. 复制模板
cp adapters/tushare.py.template adapters/my_backend.py

# 2. 编辑 my_backend.py，改三处：
#    - _DEFAULT_DB_PATH → 你的 SQLite 库路径
#    - TUSHARE_FORM 里每个 "表名.字段名" → 你库里的实际位置
#    - 可选：添加 extra_fields、增删辅助能力

# 3. 验证
python -c "
from adapters.my_backend import MyBackend
b = MyBackend()
print(b.get_calendar('20240101', '20240131'))
"
```

---

## 2. 填表法总览

填表法的核心是 `GenericSQLBackend`（`btcore/generic_sql.py`）。用户子类化它，传入一个 Python dict（"表单"）声明每份数据在自己库里的位置，其余拼表、对齐、列裁剪、CSE 全部由通用机械自动处理。

完整示例参考 `adapters/tushare.py`，模板参考 `adapters/tushare.py.template`。

### 2.1 表单结构

表单是一个扁平的 Python dict，每项的含义由键名决定：

```python
MY_FORM = {
    # ══ 索引键名 — 只填列名，不带表名（§4）══
    "symbol": "ts_code",          # 证券代码列的名字
    "date":   "trade_date",       # 交易日列的名字（YYYYMMDD 字符串）

    # ══ 引擎必需 — 每个空填 "表名.字段名"（§5）══
    "open":      "daily.open",
    "high":      "daily.high",
    "low":       "daily.low",
    "close":     "daily.close",
    "vol":       "daily.vol",
    "adj_factor":    "adj_factor.adj_factor",
    "pre_close":     "daily.pre_close",
    "up_limit":      "stk_limit.up_limit",
    "down_limit":    "stk_limit.down_limit",
    "calendar_date":      "trade_cal.cal_date",
    "dividend_ex_date":   "dividend.ex_date",
    "dividend_stk_div":   "dividend.stk_div",
    "dividend_cash_div":  "dividend.cash_div",

    # ══ 引擎辅助能力 — 不填 = 该能力关闭（§6）══
    "st_symbol":       "stock_st.ts_code",
    "industry_name":   "index_member_all.l1_name",
    "listing_date":    "stock_basic.list_date",
    "index_code":      "index_weight.index_code",
    "index_member":    "index_weight.con_code",
    "benchmark_close":      "index_daily.close",
    "benchmark_adj_factor": "fund_adj.adj_factor",  # 可选
    "benchmark_code":  "000300.SH",  # 默认基准代码（取值，非位置）

    # ══ 自选扩展字段（§7）══
    "extra_fields": {
        "amount": "daily.amount",
        "turnover_rate": "daily_basic.turnover_rate",
    },

    # ══ 表的特殊说明（§8）══
    "tables": {
        "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
        "dividend":  {"filter": {"div_proc": "实施", "ex_date": None}},
    },
}
```

### 2.2 关键规则

- **没有主表/辅助表之分**：就算 OHLC 分存在四张表也直接各填各的。引擎内部把被引用表按 `(交易日, 代码)` 做外并集拼成网格，某字段在某张表没有对应行即为 NaN。
- **能力开关 = 对应的空填没填**：填了就有该能力，不填则对应功能静默关闭。
- **所有表名/列名自动加双引号**：物理名撞 SQL 保留字（如 `limit`）的字段可直接对接。
- **VIEW 与表等价**：表单引用的"表"可以是 VIEW。例如 tushare 的 `daily` 表 `amount` 是千元，可通过 `CREATE VIEW daily_rmb AS SELECT *, amount * 1000 AS amount FROM daily;` 转换后指向 VIEW。
- **初始化期全量校验**：`__init__` 时表单引用的所有表和列都会做落库校验，拼写错误当场暴露，定位到具体表单条目。
- **运行期列名校验**：`query_bars` 被请求未在表单中声明的列名时，引擎抛出 `ValueError` 并列出具体未知列名——快速定位 `REQUIRED_FIELDS` 或 `extra_fields` 遗漏。

---

## 3. 备选方案：函数对接（手写实现）

如果数据不在 SQLite 里——内存、CSV、parquet、HTTP API 等——或者库结构过于特殊不适合填表，可以直接实现 `DataBackend` ABC。

### 3.1 必须实现的抽象方法

```python
from btcore.backend import DataBackend

class MyBackend(DataBackend):
    def query_bars(
        self,
        symbols: list[str] | None,
        start: str,
        end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        """查询 [start, end] 闭区间内的日线数据。

        返回 MultiIndex (trade_date, symbol)，日期为 YYYYMMDD 字符串。

        必需列（缺列引擎 preload 直接报错）：
        - open / high / low / close: 裸价（元）
        - vol: 成交量（手）
        - adj_factor: 后复权因子（除数法）
        - pre_close: 昨收价（交易所除权口径）
        - up_limit / down_limit: 精确涨跌停价（元）

        以下列由引擎派生，勿提供：
        - open_hfq / high_hfq / low_hfq / close_hfq / pct_chg

        扩展列作为额外列直接加入 DataFrame，前视防护自动生效。
        columns 参数为 None 时返回全部可用列。
        """
        ...

    def get_calendar(self, start: str, end: str) -> list[str]:
        """返回 [start, end] 区间内的交易日列表，YYYYMMDD 格式升序。

        必须包含 start 和 end（如果当天是交易日）。
        """
        ...

    def get_dividends_on_date(self, date_str: str) -> dict[str, dict[str, float]]:
        """返回指定日期的除权除息记录。

        返回格式：{symbol: {"stk_div": float, "cash_div": float}}
        - stk_div: 每股送转比例（10 送 3 → 0.3）
        - cash_div: 每股现金红利（元）
        - 无除权除息的日期返回空 dict。
        """
        ...
```

### 3.2 鸭子类型能力方法（按需实现）

以下方法不是抽象方法——引擎通过 `getattr(backend, "方法名", None)` 检测。不存在时对应功能自动降级。

```python
# 引擎调用 —— 不实现 = 对应功能关闭

def get_benchmark_bars(
    self, code: str | None = None, start: str = "", end: str = ""
) -> pd.DataFrame | None:
    """基准指数日线数据。
    返回 datetime 索引，columns: ["hfq_close"]。
    无数据返回 None。code 由调用方显式传入 benchmark 代码。
    """
    ...

def get_st_map(self, from_date: str) -> dict[str, set[str]]:
    """返回从 from_date 起每日的 ST 名单。
    返回 {date: {symbol, ...}}，date 为 YYYYMMDD 字符串。
    """
    ...

def get_stock_industries(self, ts_codes: list[str]) -> dict[str, str]:
    """返回股票代码到行业名称的映射。
    返回 {symbol: 行业名称}，未找到的股票不出现在结果中。
    """
    ...

def get_recent_listings(
    self, cutoff_days: int = 60, as_of: str | None = None
) -> set[str]:
    """返回近期上市的股票代码集合（上市日距 as_of ≤ cutoff_days）。
    as_of 为 None 时取当前日期。
    """
    ...

def get_index_members(
    self, index_codes: list[str], start: str, end: str
) -> dict[str, set[str]]:
    """返回指定指数在 [start, end] 区间内每日的成分股。
    返回 {date: {symbol, ...}}。
    """
    ...

### 3.3 完整示例

`tests/test_foreign_backend.py` 中的 `ForeignBackend` 是一个完整的手写实现示例：

- 数据源：内存 `list[dict]`，非 SQL
- 股票代码：`CUSTOM_001` ~ `CUSTOM_010`，非 tushare 格式
- 日历：虚构连续自然日，非 SSE 交易日历
- 价格单位是分（在 `query_bars` 内转换成元）
- `pre_close` / `up_limit` / `down_limit` 由后端按规则自行计算
- 扩展列 `sentiment_score` 来自独立维护的内存 dict
- 没有 ST、行业、指数成分——对应能力自动关闭

此示例验证了 `DataBackend` ABC 的通用性：无论数据来源和格式如何，只要实现三个抽象方法并返回正确口径的数据，引擎就能正常运行。

---

## 4. 索引键名：`symbol` 与 `date`

这两个空**只填列名，不带表名**。它们声明的是各表共同的查询键列名字——即每张表里代表证券代码和交易日的列叫什么。

```python
"symbol": "ts_code",       # 不是 "某张表.ts_code"，就是在说"我的库里代码列叫 ts_code"
"date":   "trade_date",    # 同理，"我的库里日期列叫 trade_date"
```

大多数数据源各表的键列名一致（tushare 全库统一叫 `ts_code` / `trade_date`），填完即可。如果某张表例外——比如 `repurchase` 表的日期是公告日 `ann_date` 而非交易日 `trade_date`——在 `tables` 节声明覆盖：

```python
"tables": {
    "repurchase": {"date": "ann_date"},
}
```

这样 `repurchase` 表的日期列就用 `ann_date` 参与对齐，其余表仍用全局的 `trade_date`。

---

## 5. 十三必需接口

这 13 个空是引擎运行的最小数据需求，**全部必填**，分为两组。

### 5.1 数据契约字段（9 项）

行情与价格数据。tushare pro 2000 积分档位跨 3 张表凑齐：

| 空 | 含义 | tushare 映射 (2000 积分) | 口径要求 |
|---|---|---|---|
| `open` | 开盘价（元） | daily.open | 裸价，不复权 |
| `high` | 最高价（元） | daily.high | 裸价 |
| `low` | 最低价（元） | daily.low | 裸价 |
| `close` | 收盘价（元） | daily.close | 裸价 |
| `vol` | 成交量 | daily.vol | **单位：手**（1 手 = 100 股） |
| `adj_factor` | 后复权因子 | adj_factor.adj_factor | 后复权乘数（`hfq_close = close × adj_factor`） |
| `pre_close` | 昨收价（元） | daily.pre_close | **交易所除权口径**：除权日为 `(前裸收盘 - 现金分红) / (1 + 送转比例)`，非除权日为前裸收盘 |
| `up_limit` | 涨停价（元） | stk_limit.up_limit | 精确值，不可用 ±10% 近似 |
| `down_limit` | 跌停价（元） | stk_limit.down_limit | 精确值，不可用 ±10% 近似 |

> **注意**：`up_limit` / `down_limit` 在数据源中常和 OHLCV 不在同一张表（如 tushare 的 `stk_limit`），直接填 `"stk_limit.up_limit"` 即可，引擎自动按 `(交易日, 代码)` 对齐。

**引擎自动派生的列，勿提供**：`open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` / `pct_chg`。这些列由引擎从裸价 + adj_factor 精确计算，不得在表单中声明。

**`amount`（成交额）非引擎必需**：引擎内部不消费 `amount`，策略如需要，通过 `REQUIRED_FIELDS` 声明后引擎会自动按需加载。如需接 `amount`，填在 `extra_fields` 中（见 §7）。

> **extra_fields 与引擎能力的隐式依赖**
>
> 部分 extra_fields 字段对特定引擎能力是必需的。虽然引擎不强制要求这些列，但缺少时对应功能静默失效：
> - `pe_ttm`：`exclude_loss` 过滤规则依赖（未声明 `pe_ttm` 则过滤静默跳过，不报错也不过滤）
> - `total_mv`：`log_mktcap` 伪列依赖（未声明则因子表达式中引用 `log_mktcap` 时 preload 直接报错）
> - `turnover_rate`：部分策略示例（如 topk_momentum）在 `select()` 中命令式访问此列

### 5.2 交易日历与分红（4 项）

| 空 | 含义 | tushare 映射 | 口径要求 |
|---|---|---|---|
| `calendar_date` | 交易日历日期列 | trade_cal.cal_date | YYYYMMDD 字符串 |
| `dividend_ex_date` | 除权除息日 | dividend.ex_date | YYYYMMDD 字符串 |
| `dividend_stk_div` | 每股送转比例 | dividend.stk_div | 10 送 3 → `0.3` |
| `dividend_cash_div` | 每股现金红利（元） | dividend.cash_div | — |

这四个空的特殊要求：

- **`calendar_date`**：必须能生成完整的交易日列表。tushare 的 `trade_cal` 表包含全市场日历，需通过 `tables` 节过滤上交所开市日（见 §8）。
- **`dividend_ex_date` / `dividend_stk_div` / `dividend_cash_div` 必须在同一张表**。引擎通过 `ex_date` 建索引做 O(1) 查找。
- 分红表需要过滤"实施"状态、排除 `ex_date` 为 NULL 的行（见 §8 的 filter 示例）。

---

## 6. 辅助能力接口

这 7 个空非必须——填了启用对应能力，不填则对应功能静默关闭。引擎通过 `getattr(backend, "方法名", None)` 检测。

### 6.1 ST 标记：`st_symbol`

```python
"st_symbol": "stock_st.ts_code",
```

启用能力：`exclude_st` 过滤（策略 `filter_rules` 中声明后自动过滤 ST 股）、`get_st_map()`。

**数据要求**：ST 标记表是日频快照——某股票在某日有记录 = 该日处于 ST 状态。tushare 的 `stock_st` 表需通过 `tables` 节过滤 `"type": "ST"`。

**不填的影响**：策略中配置了 `exclude_st` 时引擎告警后静默降级（ST 过滤不生效），策略继续运行。

### 6.2 行业分类：`industry_name`

```python
"industry_name": "index_member_all.l1_name",
```

启用能力：行业风控（`max_industry_pct` 控制单行业最大仓位占比）、`industry` 分组（策略 `groupby` 功能）、行业过滤（`exclude_industry`）。

**不填的影响**：`exclude_industries` 配置时引擎告警后静默降级（行业过滤不生效）；`max_industry_pct` 配置时直接报错（`ValueError`）。`industry` 伪列不可用于因子表达式。

### 6.3 上市日期：`listing_date`

```python
"listing_date": "stock_basic.list_date",
```

启用能力：`exclude_new_stock` 过滤（排除上市不足 N 天的新股，默认 60 天）。

**不填的影响**：新股过滤告警后跳过——引擎不报错（有 warning 日志），但也不会过滤新股。

### 6.4 指数成分：`index_code` + `index_member`

```python
"index_code":   "index_weight.index_code",
"index_member": "index_weight.con_code",
```

**必须成对填写**：只填一个会报错。两个空必须在同一张表。

启用能力：`index_universe`（策略级股票池限定为指数成分）、`factor_universe`（因子计算时限定股票池）。

**不填的影响**：配置了 `index_universe` 或 `factor_universe` 时引擎告警后静默降级（对应规则不生效）。

### 6.5 基准行情：`benchmark_close` + `benchmark_adj_factor` + `benchmark_code`

```python
"benchmark_close":      "index_daily.close",
"benchmark_adj_factor": "fund_adj.adj_factor",  # 可选
"benchmark_code":       "000300.SH",
```

- `benchmark_close`：基准指数的日线收盘价（或净值）。
- `benchmark_adj_factor`：复权因子，**可选**。指数点位等无复权概念的基准只填 `benchmark_close` 即可；基金净值类需要复权的基准填上。
- `benchmark_code`：默认基准代码，**字符串取值，不是位置**。引擎调用 `get_benchmark_bars()` 不传参数时用此默认值。

启用能力：基准收益统计（累计收益、年化收益、最大回撤等指标中的基准对比）、`idx_ret` 因子（基准指数日收益率，可在因子表达式中引用）。

**不填的影响**：报告中的基准对比列为空，`idx_ret` 因子不可用。引擎不报错。

---

## 7. 扩展字段：对接更多数据

`extra_fields` 是扩展因子和策略所需数据列的入口。设计更复杂的因子或策略时，需要在这里声明新字段。

```python
"extra_fields": {
    "turnover_rate": "daily_basic.turnover_rate",
    "pe_ttm": "daily_basic.pe_ttm",
    "net_mf_amount": "moneyflow.net_mf_amount",
    "winner_rate": "cyq_perf.winner_rate",
}
```

机制要点：

- **跨表无感知**：字段可以来自任意表——引擎内部将被引用表按 `(交易日, 代码)` 做外并集拼成网格。来自不同表的字段自动对齐到同一 DataFrame 上，某字段在某表没有对应行即为 NaN。
- **列名即接口名**：`extra_fields` 的键是引擎和策略消费的列名（如 `pe_ttm`），值是该列在库里的位置（`"daily_basic.pe_ttm"`）。策略的 `REQUIRED_FIELDS` 声明里直接写键名即可。
- **不可使用保留名**：`open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` / `pct_chg` / `idx_ret` / `log_mktcap` / `industry` / `symbol` / `trade_date` 由引擎派生或作为索引键，不能在 `extra_fields` 中声明。

### 7.1 扩展字段按数据来源分类

以下四类覆盖 2000 积分下最常用的扩展数据，每类列出模板中预填的代表性字段。需要更多字段时，参考 tushare 官方文档或 `tushare_db/api_index.json` 中对应 API 的 `output_params`，按同样格式追加即可。

**日频基本面**（`daily_basic` 表，2000 积分）：

| 字段 | 含义 |
|------|------|
| `turnover_rate` | 换手率（自由流通股本口径，%） |
| `pe_ttm` | 市盈率（TTM） |
| `pb` | 市净率 |
| `total_mv` | 总市值（万元） |

**资金流向**（`moneyflow` 表，2000 积分）：

| 字段 | 含义 |
|------|------|
| `net_mf_amount` | 净流向额（万元） |

**筹码分布**（`cyq_perf` 表，需 5000 积分）：

| 字段 | 含义 |
|------|------|
| `winner_rate` | 获利盘比例 |

**融资融券**（`margin_detail` 表，2000 积分）：

| 字段 | 含义 |
|------|------|
| `rzye` | 融资余额 |

> **财报与基本面数据：跨频率对齐**
>
> ddup 引擎只消费 `(交易日期, 证券代码)` 日频网格上的列，**不做季度频率推断**。财报类数据（`pe_ttm`、`eps`、`rev_yoy` 等）须在数据层按**公告日**（`ann_date`）而非报告期对齐成日频列。建议建 VIEW 或物化表：
>
> ```sql
> CREATE VIEW v_financials AS
> SELECT ts_code, ann_date AS trade_date, pe_ttm, eps, rev_yoy, ...
> FROM financials;
> ```
>
> 然后在表单里引用 VIEW：`"pe_ttm": "v_financials.pe_ttm"`。跨季度运算（如 YoY 增速）也预先算成列——引擎不会自动推断前四个季度的数据来做同比增长。

### 7.2 特殊表：日期对齐问题

部分 tushare 表的日期列不是 `trade_date`，需要通过 `tables` 节的 `date` 覆盖声明对齐列：

```python
"tables": {
    "repurchase":       {"date": "ann_date"},
    "stk_holdertrade":  {"date": "ann_date"},
    "stk_holdernumber": {"date": "end_date"},
}
```

引擎会使用声明的对齐列（`ann_date` / `end_date`）替代全局的 `trade_date` 进行日期过滤和网格对齐。事件型表（repurchase、stk_holdertrade）只在公告日有值，其余日期在拼好的网格中表现为 NaN——这符合预期，不是错误。

### 7.3 完全自定义数据源

`extra_fields` 不限于 tushare——任何有 `(代码, 日期)` 键的数据都可以对接。例如接入社交媒体情绪评分、新闻舆情数据、替代数据厂商的因子等，只要导入 SQLite 并填好映射即可：

```python
"extra_fields": {
    "sentiment_score": "social_media.sentiment",     # 社交媒体情绪
    "news_impact":     "alt_data.news_impact_score",  # 替代数据
}
```

字段来自不同表完全没问题，引擎自动做外并集对齐。NaN 处按需在因子表达式中用 `if_else` 或 `fill` 算子处理。

---

## 8. 表的特殊说明：`tables` 节

`tables` 节用于声明某张表的特殊处理，大多数库为空。支持四个子键：

| 键 | 用途 | 适用范围 |
|---|---|---|
| `filter` | 行过滤（键值对 dict） | 日历 / 分红 / ST / 指数成分表 |
| `filter_sql` | 复杂行过滤（手写 SQL 片段） | 同上 |
| `symbol` | 该表的代码列名覆盖 | 任何已引用表 |
| `date` | 该表的日期列名覆盖 | 任何已引用表 |

### 8.1 filter — 行过滤

键值对 dict，值 `None` 表示 `IS NOT NULL`。典型用法：

```python
"tables": {
    # 日历：只取上交所开市日
    "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
    # 分红：只取已实施的，且 ex_date 不为空
    "dividend":  {"filter": {"div_proc": "实施", "ex_date": None}},
    # ST 标记：只取 ST 类型，排除 *ST 等
    "stock_st":  {"filter": {"type": "ST"}},
}
```

filter 编译为 `WHERE col1 = ? AND col2 IS NOT NULL`，列名自动加双引号。

### 8.2 filter_sql — 复杂过滤

当 filter 的简单等值匹配表达不了需求时，使用 `filter_sql` 写原始 SQL 片段（直接拼接不处理）。适用于范围条件、IN 子句、函数调用等：

```python
"tables": {
    "index_weight": {"filter_sql": "weight > 0.1 AND flag = 'valid'"},
}
```

片段直接嵌入 `WHERE (...)` 中，需自行保证语法正确。

### 8.3 键列名覆盖

当某张表的代码列或日期列名与全局的 `symbol` / `date` 声明不同时：

```python
"symbol": "ts_code",        # 全局：代码列叫 ts_code
"date":   "trade_date",     # 全局：日期列叫 trade_date

"tables": {
    "repurchase": {"date": "ann_date"},     # 这张表的日期列叫 ann_date
    "weird_table": {"symbol": "code"},       # 这张表的代码列叫 code
}
```

---

## 9. 检查清单

完成对接后，逐项检查：

- [ ] `symbol` 和 `date` 填的是列名，不带表名
- [ ] 13 个必需空全部填写，格式都是 `"表名.字段名"`
- [ ] `pre_close` 确认是交易所除权口径（不是裸前收价）
- [ ] `up_limit` / `down_limit` 确认是精确值（不是 ±10% 近似）
- [ ] `vol` 确认单位是手（不是股）
- [ ] `calendar_date` 确认过滤了非交易日（tushare 需加 `exchange: SSE, is_open: 1`）
- [ ] `dividend` 表的三个空在同一张表，确认过滤了"实施"状态
- [ ] 辅助能力：需要哪项填哪项，不用的空整行删掉即可
- [ ] `index_code` 和 `index_member` 要么都填要么都不填
- [ ] 事件型表（repurchase / stk_holdertrade / stk_holdernumber）在 `tables` 节声明了正确的 `date` 覆盖
- [ ] 运行 `python -c "from adapters.my_backend import MyBackend; b = MyBackend()"` 不报错（落库校验通过）
- [ ] 运行 `python -c "from adapters.my_backend import MyBackend; print(MyBackend().get_calendar('20240101', '20240131')[:5])"` 看到正确日历

---

## 10. 能力缺失行为速查

以下对照表汇总填表法中辅助能力缺失时引擎和策略侧的精确行为。核心分界：**可选能力缺失 → 告警软回退**；**明确声明但不可用 → 直接报错**。

| 缺失项 | 引擎行为 | 检测方式 | 策略侧影响 |
|--------|---------|---------|-----------|
| ST 标记 `st_symbol` | 告警后继续运行 | 初始化期 `hasattr` | `exclude_st: true` 配置时告警后静默降级（ST 过滤不生效） |
| 行业分类 `industry_name` | 告警后继续运行 | 初始化期 `hasattr` | `exclude_industries` → 告警后静默降级；`max_industry_pct` → 直接报错；`industry` 伪列不可用 |
| 上市日期 `listing_date` | 告警后继续运行 | 初始化期 `hasattr` | `exclude_new_stock: true` 不生效，引擎不报错 |
| 指数成分 `index_code` + `index_member` | 告警后继续运行 | 初始化期 `hasattr` | `index_universe` 或 `factor_universe` 配置时告警后静默降级（对应规则不生效） |
| 基准行情 `benchmark_close` | 静默关闭 | 初始化期 `hasattr` | 基准对比列为空；`idx_ret` 因子不可用；引擎不报错 |
