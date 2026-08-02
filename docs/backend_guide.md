# 数据库对接指南

ddup 引擎通过 `DataBackend` 抽象层（`btcore/backend.py`）消费数据，两种接入方式：

| 方式 | 适用场景 | 工作量 |
|------|---------|--------|
| **A) 填表法** | 数据在 SQLite 库中（表或 VIEW） | 子类化 `GenericSQLBackend`，填一个 Python dict，零 SQL |
| **B) 手写实现** | 非 SQLite 数据源（内存、API、parquet、CSV 等） | 子类化 `DataBackend`，实现 3 个抽象方法 |

推荐填表法。§1–§2、§4–§10 讲填表法，§3 讲手写实现。

---

## 1. 快速开始

推荐搭配 [tushare_db](https://github.com/netube99/tushare_db) 维护的 SQLite 库使用，引擎开发过程已原生适配其表组织形式；匹配其他第三方库可能出现预期之外的问题。

> tushare.pro 积分要求：账户 **2000 积分**可凑齐引擎必需数据；`stock_st` 接口要求 **3000 积分**，缺少该表引擎无法正确筛选 ST 股。

```bash
# 1. 复制模板
cp adapters/tushare.py.template adapters/my_backend.py

# 2. 编辑 my_backend.py，改三处：
#    - _DEFAULT_DB_PATH → 你的 SQLite 库路径
#    - TUSHARE_FORM 里每个 "表名.字段名" → 你库里的实际位置
#    - 可选：添加 extra_fields、增删辅助能力

# 3. 验证（初始化期落库校验 + 日历查询）
python -c "
from adapters.my_backend import TushareBackend
b = TushareBackend()
print(b.get_calendar('20240101', '20240131'))
"
```

- 完整填好的示例：`adapters/tushare.py`（基于 tushare_db 的 `stk_factor_pro` 等表）。
- 可复制的空模板：`adapters/tushare.py.template`（按 tushare 原生 `daily` 体系映射，类名 `TushareBackend`）。

---

## 2. 填表法总览

子类化 `GenericSQLBackend`（`btcore/generic_sql.py`），传入一个 Python dict（"表单"）声明每份数据在库里的位置，拼表、对齐、列裁剪、校验全部由通用机械完成：

```python
from btcore.generic_sql import GenericSQLBackend

class MyBackend(GenericSQLBackend):
    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        super().__init__(MY_FORM, db_path)
```

### 2.1 表单结构

表单是扁平 dict，每项含义由键名决定：

```python
MY_FORM = {
    # ══ 索引键名 — 只填列名，不带表名（§4）══
    "symbol": "ts_code",          # 证券代码列的名字
    "date":   "trade_date",       # 交易日列的名字（YYYYMMDD 字符串）

    # ══ 十三必需 — 每个空填 "表名.字段名"（§5）══
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

    # ══ 辅助能力 — 不填 = 该能力关闭（§6）══
    "st_symbol":       "stock_st.ts_code",
    "industry_name":   "index_member_all.l1_name",
    "listing_date":    "stock_basic.list_date",
    "index_code":      "index_weight.index_code",
    "index_member":    "index_weight.con_code",
    "benchmark_close":      "index_daily.close",
    "benchmark_adj_factor": "fund_adj.adj_factor",   # 可选
    "benchmark_code":  "000300.SH",   # 默认基准代码（取值，非位置）

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

- **没有主表/辅助表之分**：OHLC 分存四张表也各填各的。被引用表按 `(交易日, 代码)` 键外并集拼成网格，某字段在某张表没有对应行即为 NaN。
- **能力开关 = 对应的空填没填**：填了就有该能力，不填则对应功能关闭（详见 §10 缺失行为）。
- **表名/列名自动加双引号**：物理名撞 SQL 保留字（如 `limit`）的字段可直接对接。
- **VIEW 与表等价**：表单引用的"表"可以是 VIEW。例如 tushare 低积分库 `daily.amount` 单位是千元，可 `CREATE VIEW daily_rmb AS SELECT *, amount * 1000 AS amount FROM daily;` 后指向 VIEW。
- **初始化期全量校验**：`__init__` 时表单引用的所有表和列做落库校验，拼写错误当场暴露，报错定位到具体表单条目。表单存在未知顶层键、`tables` 中出现未被任何字段引用的表，也在初始化期报错。
- **运行期列名校验**：`query_bars` 被请求未在表单中声明的列名时抛出 `ValueError` 并列出未知列名——用于快速定位 `REQUIRED_FIELDS` 或 `extra_fields` 遗漏。

---

## 3. 备选方案：手写实现 `DataBackend`

数据不在 SQLite（内存、CSV、parquet、HTTP API 等）或库结构不适合填表时，直接实现 `DataBackend` ABC。

### 3.1 必须实现的抽象方法（3 个）

| 方法 | 签名 | 返回契约 |
|------|------|---------|
| `query_bars` | `(symbols: list[str] \| None, start: str, end: str, columns: list[str] \| None = None) -> pd.DataFrame` | `[start, end]` 闭区间日线；MultiIndex `(trade_date, symbol)`，日期为 YYYYMMDD 字符串；`symbols=None` 表示全部股票，**空列表 `[]` 返回空面板**（与 None 语义不同，勿混用）；`columns=None` 返回全部可用列；被请求不存在的列名应快速报错，不得静默忽略 |
| `get_calendar` | `(start: str, end: str) -> list[str]` | `[start, end]` 区间内交易日列表，YYYYMMDD 升序；start/end 当天是交易日则必须包含 |
| `get_dividends_on_date` | `(date_str: str) -> dict[str, dict[str, float]]` | `{symbol: {"stk_div": float, "cash_div": float}}`；`stk_div` 每股送转比例（10 送 3 → 0.3），`cash_div` 每股现金红利（元）；当日无除权除息返回空 dict |

`query_bars` 返回的 DataFrame 必须含**数据契约列**（缺列引擎 preload 直接报错）：

| 列 | 口径 |
|---|---|
| `open` / `high` / `low` / `close` | 裸价（元），不复权 |
| `vol` | 成交量，单位**手**（1 手 = 100 股） |
| `adj_factor` | 后复权因子，引擎按 `hfq价 = 裸价 × adj_factor` 派生复权价 |
| `pre_close` | 昨收价，**交易所除权口径**：除权日 `(前裸收盘 - 现金分红) / (1 + 送转比例)`，非除权日为前裸收盘 |
| `up_limit` / `down_limit` | 精确涨跌停价（元），不可用 ±10% 近似 |

- **引擎派生列，勿提供**：`open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` / `pct_chg`，由引擎从基础列精确计算。
- **扩展列**（资金流、情绪评分等）作为额外列直接加入 DataFrame，前视防护对所有列自动生效；策略命令式访问的扩展列须声明进策略的 `REQUIRED_FIELDS`。

### 3.2 鸭子类型能力方法（按需实现）

非抽象方法。引擎通过 `getattr(backend, "方法名", None)` 检测，不存在时对应功能自动降级（行为见 §10）。签名必须匹配：

| 方法 | 签名 | 返回契约 | 支撑功能 |
|------|------|---------|---------|
| `get_benchmark_bars` | `(code: str \| None = None, start: str = "", end: str = "") -> pd.DataFrame \| None` | 单列 `hfq_close`（后复权收盘价）；日期索引（datetime 或 YYYYMMDD 字符串均可，引擎归一化）；无数据返回 `None` | 基准收益统计、`idx_ret` 因子 |
| `get_st_map` | `(from_date: str) -> dict[str, set[str]]` | `{date: {symbol, ...}}`，date 为 YYYYMMDD；日频快照，当日有记录 = 当日 ST | `exclude_st` 过滤 |
| `get_stock_industries` | `(ts_codes: list[str]) -> dict[str, str]` | `{symbol: 行业名称}`，未找到的不出现在结果中 | `industry` 分组伪列、`exclude_industries` 过滤 |
| `get_recent_listings` | `(cutoff_days: int = 60, as_of: str \| None = None) -> set[str]` | 上市日距 `as_of` ≤ `cutoff_days` 的股票代码集合；`as_of=None` 取当前日期 | `exclude_new_stock` 过滤 |
| `get_index_members` | `(index_codes: list[str], start: str, end: str) -> dict[str, set[str]]` | `{date: {symbol, ...}}`，多指数并集；快照可为月频，引擎按"最近一期 ≤ 当日"取值 | `index_universe` / `factor_universe` 股票池限定 |

### 3.3 完整示例

`tests/test_foreign_backend.py` 的 `ForeignBackend` 是一个完整手写实现：

- 数据源为内存 `list[dict]`，非 SQL；股票代码 `CUSTOM_001`~`CUSTOM_010`，非 tushare 格式；日历为虚构连续自然日
- 价格原始单位是分，在 `query_bars` 内转换成元；`pre_close` / `up_limit` / `down_limit` 由后端按规则自行计算
- 扩展列 `sentiment_score` 来自独立维护的内存 dict，在 `query_bars` 里 join 进 DataFrame
- 不实现任何能力方法——ST、行业、指数成分、基准能力自动关闭

结论：无论数据来源和格式如何，实现三个抽象方法并返回正确口径的数据，引擎即可正常运行。

---

## 4. 索引键名：`symbol` 与 `date`

这两个空**只填列名，不带表名**——声明各表共同的查询键列名字：

```python
"symbol": "ts_code",       # 我的库里代码列叫 ts_code
"date":   "trade_date",    # 我的库里日期列叫 trade_date
```

某张表例外时（如 `repurchase` 表的日期是公告日 `ann_date`），在 `tables` 节覆盖（§8.3）：

```python
"tables": {
    "repurchase": {"date": "ann_date"},
}
```

---

## 5. 十三必需接口

13 个空是引擎运行的最小数据需求，**全部必填**（缺任一项初始化报错）。

### 5.1 数据契约字段（9 项）

| 空 | 含义 | tushare 映射 | 口径要求 |
|---|---|---|---|
| `open` | 开盘价（元） | `daily.open` | 裸价，不复权 |
| `high` | 最高价（元） | `daily.high` | 裸价 |
| `low` | 最低价（元） | `daily.low` | 裸价 |
| `close` | 收盘价（元） | `daily.close` | 裸价 |
| `vol` | 成交量 | `daily.vol` | 单位**手** |
| `adj_factor` | 后复权因子 | `adj_factor.adj_factor` | 引擎按 `hfq价 = 裸价 × adj_factor` 派生 |
| `pre_close` | 昨收价（元） | `daily.pre_close` | **交易所除权口径**（§3.1） |
| `up_limit` | 涨停价（元） | `stk_limit.up_limit` | 精确值，不可用 ±10% 近似 |
| `down_limit` | 跌停价（元） | `stk_limit.down_limit` | 精确值 |

- 涨跌停与 OHLCV 不在同一张表是常态，直接填 `"stk_limit.up_limit"`，引擎按 `(交易日, 代码)` 自动对齐。
- 引擎派生列 `open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` / `pct_chg` 不得在表单中声明。
- **`amount` 非引擎必需**：引擎内部不消费成交额，策略需要时通过 `REQUIRED_FIELDS` 声明；对接填在 `extra_fields`（§7）。tushare 低积分库 `daily.amount` 单位是千元，可用 VIEW 转换（§2.2）。

> **extra_fields 与引擎能力的隐式依赖**
>
> 以下扩展字段虽非强制，但缺少时对应功能降级或报错：
>
> | 字段 | 依赖方 | 缺失行为 |
> |---|---|---|
> | `eps` | `exclude_loss` 过滤规则 | 告警一次，亏损过滤不生效（显式声明 `exclude_loss: true` 可让引擎 preload 该列；后端无 `eps` 时回退 `pe_ttm<=0`）。注：tushare 亏损股 `pe_ttm` 为 NULL 或正数，`eps<0` 才是可靠亏损信号 |
> | `total_mv` | `log_mktcap` 伪列 | 因子表达式引用 `log_mktcap` 时 preload 报错 |
> | `turnover_rate` 等任意列 | 策略 `select()` / `on_tick()` 命令式访问 | 未声明进 `REQUIRED_FIELDS` 时列被裁掉（示例见 `strategies/examples/multi_model`） |

### 5.2 交易日历与分红（4 项）

| 空 | 含义 | tushare 映射 | 口径要求 |
|---|---|---|---|
| `calendar_date` | 交易日历日期列 | `trade_cal.cal_date` | YYYYMMDD 字符串；须过滤只留开市日（§8.1） |
| `dividend_ex_date` | 除权除息日 | `dividend.ex_date` | YYYYMMDD 字符串 |
| `dividend_stk_div` | 每股送转比例 | `dividend.stk_div` | 10 送 3 → `0.3` |
| `dividend_cash_div` | 每股现金红利（元） | `dividend.cash_div` | — |

特殊要求：

- **分红三个空必须在同一张表**（初始化期校验，否则报错）。
- 分红表需过滤"实施"状态、排除 `ex_date` 为 NULL 的行（§8.1）。

---

## 6. 辅助能力接口（可选）

7 个空，填了启用对应能力，不填则功能关闭（缺失行为汇总见 §10）。填表法中能力方法由后端按已填的空自动装配，鸭子类型检测对手写后端与填表后端一视同仁。

| 空 | 格式 | 启用能力 | 数据要求 |
|---|---|---|---|
| `st_symbol` | ST 表的代码列位置 | `exclude_st` 过滤 | 日频快照表：某日有记录 = 该日 ST；tushare `stock_st` 需 filter `"type": "ST"` |
| `industry_name` | 行业分类列位置 | `industry` 分组伪列、`exclude_industries` 过滤 | 代码 → 行业名称映射 |
| `listing_date` | 上市日期列位置 | `exclude_new_stock` 过滤（默认排除上市 ≤ 60 天） | YYYYMMDD |
| `index_code` + `index_member` | 指数代码列 + 成分股代码列 | `index_universe` / `factor_universe` 股票池限定 | **必须成对填写且在同一张表**，否则初始化报错；成分快照可为月频 |
| `benchmark_close` | 基准收盘价列位置 | 基准收益统计、`idx_ret` 因子 | 基准的日线收盘价或净值 |
| `benchmark_adj_factor` | 基准复权因子列位置（**可选**） | 基金净值类基准的复权 | 指数点位等无复权概念的基准不填，直接用 `benchmark_close`；不能脱离 `benchmark_close` 单独填写 |
| `benchmark_code` | **字符串取值，非位置**（如 `"000300.SH"`） | 调用 `get_benchmark_bars()` 不传 code 时的默认基准 | 引擎回测时总会显式传入基准代码（配置项 `benchmark`；未配置时从单指数 `index_universe` 推导，否则回退 `000300.SH`；配置空字符串表示无基准） |

---

## 7. 扩展字段：`extra_fields`

对接因子和策略所需的更多数据列：

```python
"extra_fields": {
    "turnover_rate": "daily_basic.turnover_rate",
    "pe_ttm":        "daily_basic.pe_ttm",
    "net_mf_amount": "moneyflow.net_mf_amount",
    "winner_rate":   "cyq_perf.winner_rate",
}
```

机制要点：

- **列名即接口名**：键是引擎和策略消费的列名，值是 `"表名.字段名"` 位置。策略 `REQUIRED_FIELDS` 直接写键名。
- **跨表无感知**：字段可来自任意表，自动按 `(交易日, 代码)` 外并集对齐，无对应行为 NaN。
- **保留名不可用**：`open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` / `pct_chg` / `idx_ret` / `log_mktcap` / `industry` / `symbol` / `trade_date`（引擎派生或索引键），以及表单顶层键名（`open`、`close`、`st_symbol` 等），均不能作为扩展字段名，填写会在初始化期报错。

### 7.1 常用扩展数据分类

模板中预填了以下代表字段（注释状态，按需启用）。更多字段参考 tushare 官方文档或 `tushare_db/api_index.json` 中对应 API 的 `output_params`，按同样格式追加。

| 类别 | 表 | 代表字段 |
|------|---|---------|
| 日频基本面 | `daily_basic`（2000 积分） | `turnover_rate` 换手率、`pe_ttm` 市盈率 TTM、`pb` 市净率、`total_mv` 总市值（万元） |
| 资金流向 | `moneyflow`（2000 积分） | `net_mf_amount` 净流向额（万元） |
| 筹码分布 | `cyq_perf`（5000 积分） | `winner_rate` 获利盘比例 |
| 融资融券 | `margin_detail`（2000 积分） | `rzye` 融资余额 |

仓库自带的 `adapters/tushare.py` 是完整示例：基本面/估值、技术指标、资金流向、筹码、融资融券、涨跌停、龙虎榜、大宗交易、回购、股东增减持等数百个字段的填法均可直接参照。

### 7.2 财报类数据：公告日对齐日频

引擎只消费 `(交易日期, 证券代码)` 日频网格上的列，**不做季度频率推断**。财报类数据（`eps`、`rev_yoy` 等）须在数据层按**公告日**（`ann_date`）对齐成日频列。建议建 VIEW：

```sql
CREATE VIEW v_financials AS
SELECT ts_code, ann_date AS trade_date, eps, rev_yoy, ...
FROM financials;
```

然后表单引用 VIEW：`"eps": "v_financials.eps"`。跨季度运算（如 YoY 增速）也预先算成列——引擎不会自动推断历史季度数据。

### 7.3 事件型表：日期列名覆盖

部分表的日期列不是交易日列，用 `tables` 节的 `date` 键声明对齐列：

```python
"tables": {
    "repurchase":       {"date": "ann_date"},
    "stk_holdertrade":  {"date": "ann_date"},
    "stk_holdernumber": {"date": "end_date"},
}
```

事件型表只在公告日有值，其余日期在网格中为 NaN——符合预期，不是错误。需要填充或条件取值时在数据层（SQL VIEW）预处理，或在因子表达式里用 `where` 限定有效域。

### 7.4 完全自定义数据源

任何有 `(代码, 日期)` 键的数据都可对接（社交媒体情绪、新闻舆情、替代数据厂商因子等），导入 SQLite 后填映射即可：

```python
"extra_fields": {
    "sentiment_score": "social_media.sentiment",
    "news_impact":     "alt_data.news_impact_score",
}
```

---

## 8. 表的特殊说明：`tables` 节

大多数库为空。支持 4 个子键，其余键名初始化期报错：

| 键 | 用途 | 适用范围 |
|---|---|---|
| `filter` | 行过滤（`{列: 值}` dict） | 仅日历 / 分红 / ST / 指数成分表 |
| `filter_sql` | 复杂行过滤（手写 SQL 片段） | 仅日历 / 分红 / ST / 指数成分表 |
| `symbol` | 该表代码列名覆盖 | 任何已被字段引用的表 |
| `date` | 该表日期列名覆盖 | 任何已被字段引用的表 |

对非适用范围的表配置 `filter` / `filter_sql`，或对未被任何字段引用的表配置任何子键，均在初始化期报错。

### 8.1 `filter` — 等值行过滤

值 `None` 表示 `IS NOT NULL`；编译为 `WHERE col1 = ? AND col2 IS NOT NULL`，列名自动加双引号：

```python
"tables": {
    "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},   # 只取上交所开市日
    "dividend":  {"filter": {"div_proc": "实施", "ex_date": None}}, # 已实施且日期非空
    "stock_st":  {"filter": {"type": "ST"}},                       # 只取 ST，排除 *ST 等
}
```

### 8.2 `filter_sql` — 复杂过滤

等值匹配表达不了时写原始 SQL 片段（原文嵌入 `WHERE (...)`，不处理，自行保证语法正确）：

```python
"tables": {
    "index_weight": {"filter_sql": "weight > 0.1 AND flag = 'valid'"},
}
```

### 8.3 键列名覆盖

某张表的代码列或日期列与全局 `symbol` / `date` 声明不同时：

```python
"tables": {
    "repurchase":  {"date": "ann_date"},   # 日期列覆盖
    "weird_table": {"symbol": "code"},     # 代码列覆盖
}
```

---

## 9. 检查清单

- [ ] `symbol` 和 `date` 填的是列名，不带表名
- [ ] 13 个必需空全部填写，格式都是 `"表名.字段名"`
- [ ] `pre_close` 是交易所除权口径（不是裸前收价）
- [ ] `up_limit` / `down_limit` 是精确值（不是 ±10% 近似）
- [ ] `vol` 单位是手（不是股）
- [ ] `calendar_date` 过滤了非交易日（tushare 需 `exchange: SSE, is_open: 1`）
- [ ] 分红三个空在同一张表，且过滤了"实施"状态
- [ ] 辅助能力按需填写，不用的空整行删掉
- [ ] `index_code` 和 `index_member` 要么都填（同一张表）要么都不填
- [ ] 事件型表（repurchase / stk_holdertrade / stk_holdernumber 等）在 `tables` 节声明了 `date` 覆盖
- [ ] 实例化后端不报错：`python -c "from adapters.my_backend import TushareBackend; TushareBackend()"`（落库校验通过）
- [ ] 日历正确：`python -c "from adapters.my_backend import TushareBackend; print(TushareBackend().get_calendar('20240101', '20240131')[:5])"`

---

## 10. 能力缺失行为速查

核心分界：**可选能力缺失 → 告警软回退**；**因子表达式显式引用不可用的伪列 → 直接报错**。检测方式均为鸭子类型（`getattr(backend, "方法名", None)`）。

| 缺失项 | 能力方法 | 策略配置时的行为 |
|--------|---------|----------------|
| ST 标记 `st_symbol` | `get_st_map` | `exclude_st: true` → 告警一次，ST 过滤不生效，回测继续 |
| 行业分类 `industry_name` | `get_stock_industries` | `exclude_industries` → 告警一次，行业过滤不生效；因子表达式引用 `industry` 伪列 → preload 报错 |
| 上市日期 `listing_date` | `get_recent_listings` | `exclude_new_stock: true` → 告警一次，新股过滤不生效，回测继续 |
| 指数成分 `index_code` + `index_member` | `get_index_members` | `index_universe` → 告警一次，白名单不生效；`factor_universe` → 告警一次，因子计算域回退为交易域 |
| 基准行情 `benchmark_close` | `get_benchmark_bars` | 报告基准对比列为空，不报错；因子表达式引用 `idx_ret` → preload 报错 |

另：`exclude_st` / `exclude_new_stock` / `exclude_loss` 三个布尔过滤规则**默认关闭**——策略未声明 = 不过滤、也不告警；只有显式开启且后端缺能力（缺 ST 表 / 上市日期 / `eps` 列）时才会出现上表告警（软回退）。

### 10.1 数据卫生检查与空值语义（fail-fast）

与软回退相反，以下问题在 `GenericSQLBackend` 加载期直接 `ValueError`，不会静默跑出错误结果：

- **键列类型探针**：日期键列须为 `YYYYMMDD` 文本、代码键列须为 `TEXT`（首次连接时逐表抽样验证）。SQLite 类型序 `INTEGER < TEXT`，键列存成整数时与文本参数的比较恒假，查询会静默返回空面板——回测会在“无行情数据”下照常跑完。
- **重复键检查**：任何面板表的 `(交易日, 代码)` 出现重复即报错（提示前 3 条示例）。重复键会让 outer join 多对多爆炸、策略层 `to_dict` 静默丢行。
- **空 universe 语义**：`query_bars` 的 `symbols=[]` 返回空面板，`None` 才表示全市场（引擎 preload 用 None；勿用空列表表达“全市场”）。
