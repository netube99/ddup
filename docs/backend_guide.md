# 数据库对接指南

ddup 引擎通过 `DataBackend` 抽象层（`btcore/backend.py`）消费数据，两种接入方式：

| 方式 | 适用场景 | 工作量 |
|------|---------|--------|
| **A) 填表法** | 数据在 DuckDB 库中（表或 VIEW） | 子类化 `GenericSQLBackend`，填一个 Python dict，零 SQL |
| **B) 手写实现** | 非 DuckDB 数据源（内存、API、parquet、CSV 等） | 子类化 `DataBackend`，实现 3 个抽象方法 |

DuckDB 库优先使用填表法；非 DuckDB 数据源见“手写实现 DataBackend”。

回测结果清单会记录可识别的数据源路径，以及 backend 可选提供的 `snapshot_id`。若后端
能提供不可变数据快照版本，可设置 `self.snapshot_id` 为稳定字符串；它只作为运行追溯标识，
引擎不会据此校验行情内容。GenericSQLBackend 记录数据库路径、文件大小和修改时间作为
弱身份提示，不会散列整库；手写后端未暴露路径或快照标识时，数据身份为 `unknown`。

---

## 1. 快速开始

推荐搭配 [tushare_db](https://github.com/netube99/tushare_db) 维护的 DuckDB 库使用，引擎开发过程已原生适配其表组织形式；匹配其他第三方库可能出现预期之外的问题。

> tushare.pro 积分要求：账户 **2000 积分**可凑齐引擎必需数据；`stock_st` 接口要求 **3000 积分**，缺少该表引擎无法正确筛选 ST 股。

```bash
# 1. 复制模板
cp adapters/tushare.py.template adapters/my_backend.py

# 2. 编辑 my_backend.py，改三处：
#    - _DEFAULT_DB_PATH → 你的 DuckDB 库路径（如 market.duckdb）
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

### 1.1 运行时选择后端（多后端项目）

CLI 缺省使用 `adapters.tushare:TushareBackend`；用环境变量 `DDUP_BACKEND`
可在不改代码的情况下切换后端（格式 `module:Class`）。适用场景：同一项目里
有多个数据域（各自一个 DuckDB 库），`run.py` / `factor_eval.py` / `sweep.py` /
`live.py` 等所有经 `research/cli_common.make_provider()` 的入口都遵守该变量。

---

## 2. 填表法总览

子类化 `GenericSQLBackend`（`btcore/generic_sql.py`），传入一个 Python dict（"表单"）声明每份数据在库里的位置，拼表、对齐、列裁剪、校验全部由通用机械完成：

```python
from btcore.generic_sql import GenericSQLBackend

class MyBackend(GenericSQLBackend):
    def __init__(self, db_path: str = _DEFAULT_DB_PATH):
        super().__init__(MY_FORM, db_path)
```

数据源两种进入方式（构造参数二选一，同时传/都不传在初始化期报错）：

- `db_path`：数据已在一个 DuckDB 文件里（只读打开，带写锁重试与魔数校验）；
- `conn`：注入已有 DuckDB 连接——上游是 SQLite/CSV/parquet 等异构源时，
  由你经 `ATTACH ... (TYPE SQLITE)` 或 `read_csv` VIEW 挂进连接后注入，
  表单照常填位置（ATTACH 库用 `库名.表名.字段名` 三段式）。注入连接归
  调用方持有：后端 `close()` 只注销内部视图，不关连接。完整示例见 §2.3。

### 2.1 表单结构

表单是扁平 dict，每项含义由键名决定：

```python
MY_FORM = {
    # 索引键名：只填列名，不带表名
    "symbol": "ts_code",          # 证券代码列的名字
    "date":   "trade_date",       # 交易日列的名字（YYYYMMDD 字符串）

    # 13 个必需位置：填 "表名.字段名"
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

    # 可选辅助能力：不填即关闭
    "st_symbol":       "stock_st.ts_code",
    "industry_name":   "index_member_all.l1_name",
    "listing_date":    "stock_basic.list_date",
    "index_code":      "index_weight.index_code",
    "index_member":    "index_weight.con_code",
    "benchmark_close":      "index_daily.close",
    "benchmark_adj_factor": "fund_adj.adj_factor",   # 可选
    "benchmark_code":  "000300.SH",   # 默认基准代码（取值，非位置）

    # 自选扩展字段
    "extra_fields": {
        "amount": "daily.amount",
        "turnover_rate": "daily_basic.turnover_rate",
    },

    # 表的特殊说明
    "tables": {
        "trade_cal": {"filter": {"exchange": "SSE", "is_open": 1}},
        "dividend":  {"filter": {"div_proc": "实施", "ex_date": None}},
    },
}
```

### 2.2 关键规则

- **没有主表/辅助表之分**：OHLC 分存四张表也各填各的。被引用表按 `(交易日, 代码)` 键外并集拼成网格，某字段在某张表没有对应行即为 NaN。
- **能力开关 = 对应的空填没填**：填了就有该能力，不填则对应功能关闭（见“能力缺失行为速查”）。
- **表名/列名自动加双引号**：物理名撞 SQL 保留字（如 `limit`）的字段可直接对接。位置空支持三段式 `库名.表名.字段名`（ATTACH 场景，见 §2.3）。
- **VIEW 与表等价**：表单引用的"表"可以是 VIEW。例如 tushare 低积分库 `daily.amount` 单位是千元，可 `CREATE VIEW daily_rmb AS SELECT *, amount * 1000 AS amount FROM daily;` 后指向 VIEW。
- **初始化期全量校验**：`__init__` 时表单引用的所有表和列做落库校验，拼写错误当场暴露，报错定位到具体表单条目。表单存在未知顶层键、`tables` 中出现未被任何字段引用的表，也在初始化期报错。
- **运行期列名校验**：`query_bars` 被请求未在表单中声明的列名时抛出 `ValueError` 并列出未知列名——用于快速定位 `REQUIRED_FIELDS` 或 `extra_fields` 遗漏。

### 2.3 异构上游：SQLite ATTACH 与 CSV VIEW（连接注入）

数据不在 DuckDB 文件里时不必先转库：DuckDB 能把 SQLite 库 ATTACH 进连接、把 CSV/parquet 读成 VIEW，填表法的全部机械（拼表、校验、列裁剪）照常工作。

**Recipe A：SQLite 上游（三段式表单）**

```python
import duckdb
from btcore.generic_sql import GenericSQLBackend

conn = duckdb.connect()
conn.execute("ATTACH 'his_data.db' AS src (TYPE SQLITE)")

SQLITE_FORM = {
    "symbol": "ts_code",
    "date": "trade_date",
    "open": "src.quotes.open",          # 三段式：库名.表名.字段名
    "high": "src.quotes.high",
    # ...其余契约字段同理；两段式 "表名.字段名" 行为不变
    "calendar_date": "src.trade_cal.cal_date",
    "dividend_ex_date": "src.dividend.ex_date",
    "dividend_stk_div": "src.dividend.stk_div",
    "dividend_cash_div": "src.dividend.cash_div",
}

backend = GenericSQLBackend(SQLITE_FORM, conn=conn)   # 注入连接
# ... 回测/研究正常使用 backend；结束时任一：
backend.close()   # 只注销内部视图，不关 conn
conn.close()      # 连接归调用方持有，由你关闭
```

同库同名表在 ATTACH 后不会混淆：无前缀的表单条目只匹配当前库，
SQLite 库的表必须显式带 `src.` 前缀。

**Recipe B：CSV 目录 + 清洗层（VIEW 内做数据卫生）**

散装 CSV 的典型脏法：代码丢前导零（Excel 打开过）、日期带横线、
无涨跌停列。清洗全部在 VIEW 层一次性表达，表单指向 VIEW：

```python
import duckdb

conn = duckdb.connect()
# 脏 CSV：code=1 应为 000001.SZ；dt 是 ISO 日期；无涨跌停列
conn.execute("""
CREATE VIEW quotes AS SELECT
    CASE WHEN code < 100000
         THEN printf('%06d.SZ', code) ELSE printf('%06d.SH', code)
    END AS ts_code,
    strftime(dt, '%Y%m%d') AS trade_date,
    open, high, low, close, vol, adj_factor, pre_close,
    CAST(NULL AS DOUBLE) AS up_limit,    -- 缺失涨跌停 = NaN，
    CAST(NULL AS DOUBLE) AS down_limit   -- 撮合回退 limits 板块规则推导
FROM read_csv_auto('quotes/*.csv', header=true);
CREATE VIEW cal AS SELECT CAST(cal_date AS VARCHAR) AS cal_date
FROM read_csv('cal.csv', header=true);
""")
# 表单照常两段式填："open": "quotes.open", "calendar_date": "cal.cal_date", ...
```

清洗层的两条硬约束（初始化期键类型校验强制）：

- 键列（代码/日期）必须是 `VARCHAR` 且日期为 8 位数字 `YYYYMMDD`。
  `read_csv` 会把无横线日期和纯数字代码推断成 `BIGINT`、把 ISO 日期
  推断成 `DATE`——不经 cast 直接对接会在构造后端时被拒绝，不会静默错算。
- Excel 建议先另存为 CSV（DuckDB 无原生 Excel reader）；多 sheet 先拆文件。

能力空（ST/行业/成分/基准）同样可用三段式或 VIEW，例如
`"benchmark_close": "src.index_daily.close"`。可运行完整示例见
`tests/test_backend_attach.py`。

---

## 3. 备选方案：手写实现 `DataBackend`

数据无法经 DuckDB 挂载（内存对象、HTTP API 等）、或库结构不适合填表时，直接实现 `DataBackend` ABC。CSV/parquet/SQLite 等文件型上游优先走 §2.3 的连接注入填表法。

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

非抽象方法。引擎通过 `getattr(backend, "方法名", None)` 检测，不存在时对应功能自动降级（见“能力缺失行为速查”）。签名必须匹配：

| 方法 | 签名 | 返回契约 | 支撑功能 |
|------|------|---------|---------|
| `get_benchmark_bars` | `(code: str \| None = None, start: str = "", end: str = "") -> pd.DataFrame \| None` | 单列 `hfq_close`（后复权收盘价）；日期索引（datetime 或 YYYYMMDD 字符串均可，引擎归一化）；无数据返回 `None` | 基准收益统计、`idx_ret` 因子 |
| `get_st_map` | `(from_date: str) -> dict[str, set[str]]` | `{date: {symbol, ...}}`，date 为 YYYYMMDD；日频快照，当日有记录 = 当日 ST | `exclude_st` 过滤 |
| `get_stock_industries` | `(ts_codes: list[str]) -> dict[str, str]` | `{symbol: 行业名称}`，未找到的不出现在结果中 | `industry` 分组伪列、`exclude_industries` 过滤 |
| `get_recent_listings` | `(cutoff_days: int = 60, as_of: str \| None = None) -> set[str]` | 上市日距 `as_of` ≤ `cutoff_days` 的股票代码集合；`as_of=None` 取当前日期 | `exclude_new_stock` 过滤 |
| `get_index_members` | `(index_codes: list[str], start: str, end: str) -> dict[str, set[str]]` | `{date: {symbol, ...}}`，多指数并集；快照可为月频，引擎按"最近一期 ≤ 当日"取值 | `index_universe` / `factor_universe` 股票池限定 |
| `backend_bar_columns` | `() -> set[str]` | 后端可供给的 bars 列名（canonical 名，与 `query_bars` 校验口径一致）；不实现 = 引擎按"全部可供给"处理 | preload 列裁剪：`exclude_loss` 等过滤规则依赖列与后端能力取交集，缺列（如 `eps`）时由 filters 逐 bar 软回退（见“能力缺失行为速查”） |

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

某张表例外时（如 `repurchase` 表的日期是公告日 `ann_date`），在 `tables` 节覆盖（见“键列名覆盖”）：

```python
"tables": {
    "repurchase": {"date": "ann_date"},
}
```

---

## 5. 13 个必需接口

13 个位置是引擎运行的最小数据需求，**全部必填**（缺任一项初始化报错）。

### 5.1 数据契约字段（9 项）

| 空 | 含义 | tushare 映射 | 口径要求 |
|---|---|---|---|
| `open` | 开盘价（元） | `daily.open` | 裸价，不复权 |
| `high` | 最高价（元） | `daily.high` | 裸价 |
| `low` | 最低价（元） | `daily.low` | 裸价 |
| `close` | 收盘价（元） | `daily.close` | 裸价 |
| `vol` | 成交量 | `daily.vol` | 单位**手** |
| `adj_factor` | 后复权因子 | `adj_factor.adj_factor` | 引擎按 `hfq价 = 裸价 × adj_factor` 派生 |
| `pre_close` | 昨收价（元） | `daily.pre_close` | **交易所除权口径**（见 query_bars 数据契约） |
| `up_limit` | 涨停价（元） | `stk_limit.up_limit` | 精确值，不可用 ±10% 近似 |
| `down_limit` | 跌停价（元） | `stk_limit.down_limit` | 精确值 |

- 涨跌停与 OHLCV 不在同一张表是常态，直接填 `"stk_limit.up_limit"`，引擎按 `(交易日, 代码)` 自动对齐。
- 引擎派生列 `open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` / `pct_chg` 不得在表单中声明。
- **`amount` 非引擎必需**：引擎内部不消费成交额，策略需要时通过 `REQUIRED_FIELDS` 声明；对接填在 `extra_fields`。tushare 低积分库 `daily.amount` 单位是千元，可用 VIEW 转换。

> **extra_fields 与引擎能力的隐式依赖**
>
> 以下扩展字段虽非强制，但缺少时对应功能降级或报错：
>
> | 字段 | 依赖方 | 缺失行为 |
> |---|---|---|
> | `eps` | `exclude_loss` 过滤规则 | 告警一次，本次不做亏损过滤（规则来自 YAML `filter_rules` 时需后端表单提供 `eps` 列；在策略代码里临时开启时需写进 `filter_rules` 由引擎 preload）。注：tushare 亏损股 `pe_ttm` 为 NULL 或正数，`eps<0` 才是可靠亏损信号；`pe_ttm<=0` 回退只对发布负 PE 的后端有效 |
> | `total_mv` | `log_mktcap` 伪列 | 因子表达式引用 `log_mktcap` 时 preload 报错 |
> | `turnover_rate` 等任意列 | 策略 `select()` / `on_tick()` 命令式访问 | 未声明进 `REQUIRED_FIELDS` 时列被裁掉（示例见 `strategies/examples/multi_model`） |

### 5.2 交易日历与分红（4 项）

| 空 | 含义 | tushare 映射 | 口径要求 |
|---|---|---|---|
| `calendar_date` | 交易日历日期列 | `trade_cal.cal_date` | YYYYMMDD 字符串；须过滤只留开市日（见 filter 行过滤） |
| `dividend_ex_date` | 除权除息日 | `dividend.ex_date` | YYYYMMDD 字符串 |
| `dividend_stk_div` | 每股送转比例 | `dividend.stk_div` | 10 送 3 → `0.3` |
| `dividend_cash_div` | 每股现金红利（元） | `dividend.cash_div` | — |

特殊要求：

- **分红三个空必须在同一张表**（初始化期校验，否则报错）。
- 分红表需过滤"实施"状态、排除 `ex_date` 为 NULL 的行（见 filter 行过滤）。

---

## 6. 辅助能力接口（可选）

7 个空，填了启用对应能力，不填则功能关闭（缺失行为见本章“能力缺失行为速查”）。填表法中能力方法由后端按已填的空自动装配，鸭子类型检测对手写后端与填表后端一视同仁。

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

<a id="alignment"></a>
### 7.2 财报与事件的可得日对齐

引擎只做 `(交易日, symbol)` 精确键拼接，不会把季度、公告事件自动变成日频序列。
后端需先按公告可得时点将财报映射到交易日网格，再按需要向后延续已知值；不能按
报告期提前使用，也不能仅 `ann_date AS trade_date` 后宣称已经完成日频对齐。公告在
非交易日或盘后时，须定义何时可进入 T 日收盘决策。YoY 等跨季度指标也在数据层准备。

事件型列若只记录事件当日，其余日为 NaN；“未发生”与“数据缺失”须区分。
`tables: {repurchase: {date: "ann_date"}}` 只覆盖日期列名，不会前移公告或填充缺口。
一对多事件先按明确规则聚合为日频唯一键，再接入，避免外连接放大行数。当前模板使用
`stk_holdertrade_agg` 与 `stk_holdernumber_agg` 等 VIEW 完成这种聚合。

### 7.3 完全自定义数据源

任何有 `(代码, 日期)` 键的数据都可映射，包括新闻、情绪和替代数据：

```python
"extra_fields": {
    "sentiment_score": "social_media.sentiment",
    "news_impact": "alt_data.news_impact_score",
}
```

---

## 8. 表的特殊说明：`tables` 节

大多数库为空。支持 4 个子键，其余键名初始化期报错：

| 键 | 用途 | 适用范围 |
|---|---|---|
| `filter` | 行过滤（`{列: 值}` dict） | 仅日历 / 分红 / ST / 指数成分表 |
| `filter_sql` | 复杂行过滤（手写 SQL 片段） | 仅日历 / 分红 / ST / 指数成分表 |
| `symbol` | 该表代码列名覆盖 | 任何已被字段引用的表（含分红 / ST / 行业 / 上市日期 / 指数成分 / 基准） |
| `date` | 该表日期列名覆盖 | 仅 panel 表、ST、指数成分、基准表；**对日历与分红表的日期列无效**（见下） |

对非适用范围的表配置 `filter` / `filter_sql`，或对未被任何字段引用的表配置任何子键，均在初始化期报错。`date` 覆盖落在日历/分红表上**不报错也不生效**——这两个角色的日期列由 `calendar_date` / `dividend_ex_date` 表单位置绑定，请改表单而不是 `tables`。同一张表同时充当 panel 表（被 `open` / `extra_fields` 等引用）时，`date` 覆盖仍对 panel 查询生效。

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
- [ ] 事件型表及其聚合 VIEW 用 `tables.date` 声明公告日列
- [ ] 实例化后端不报错：`python -c "from adapters.my_backend import TushareBackend; TushareBackend()"`（落库校验通过）
- [ ] 日历正确：`python -c "from adapters.my_backend import TushareBackend; print(TushareBackend().get_calendar('20240101', '20240131')[:5])"`

---

<a id="failure-modes"></a>
## 10. 能力缺失行为速查

核心分界：**可选能力缺失 → 告警软回退**；**因子表达式显式引用不可用的伪列 → 直接报错**。检测方式均为鸭子类型（`getattr(backend, "方法名", None)`）。

| 缺失项 | 能力方法 | 策略配置时的行为 |
|--------|---------|----------------|
| ST 标记 `st_symbol` | `get_st_map` | `exclude_st: true` → 告警一次，ST 过滤不生效，回测继续 |
| 行业分类 `industry_name` | `get_stock_industries` | `exclude_industries` → 告警一次，行业过滤不生效；因子表达式引用 `industry` 伪列 → preload 报错 |
| 上市日期 `listing_date` | `get_recent_listings` | `exclude_new_stock: true` → 告警一次，新股过滤不生效，回测继续 |
| 指数成分 `index_code` + `index_member` | `get_index_members` | `index_universe` → 缺方法时两处各告警一次（preload universe 裁剪回退全市场、`StockFilter` 白名单不生效）；`factor_universe` → preload 告警一次，因子计算域回退交易域 |
| 基准行情 `benchmark_close` | `get_benchmark_bars` | 报告基准对比列为空，不报错；因子表达式引用 `idx_ret` → preload 报错 |
| 列供给能力 | `backend_bar_columns` | 不实现 = 过滤规则依赖列全部照常请求，缺列由后端 `query_bars` 自行处理；实现后，`exclude_loss` 等过滤依赖列先与后端能力取交集，缺列走逐 bar 软回退 |

另：`exclude_st` / `exclude_new_stock` / `exclude_loss` 三个布尔过滤规则**默认关闭**——策略未声明 = 不过滤、也不告警；只有显式开启且后端缺能力（缺 ST 表 / 上市日期 / `eps` 列）时才会出现上表告警（软回退）。`exclude_loss` 的两类缺失各有独立告警：**列整列缺失**（后端表单未提供、引擎列协商让位）→ StockFilter 运行时告警一次，明说等价于不做亏损过滤；**列在但个别行缺**（`bak_basic` 无该股该日行）→ 引擎 preload 汇总告警一次（`filters.audit_loss_coverage`，只计有行情样本，北交所行数单独列出），单行缺 `eps` 时回退 `pe_ttm <= 0` 再判定，两列皆不可判定则保留。

指数成分的两条路径各自独立，告警次数与阶段不同：

- **preload 裁剪**（`strategy_loader._attach_universe` → `filters.resolve_index_universe`）。缺方法 → hook 首次调用时告警一次（每策略实例最多一次），回退全市场；方法在但 `[start-45d, end]` 与全历史回退都查不到快照 → **静默**返回 `None`，同样回退全市场，不告警。仅在策略未自定义 `get_universe` / `get_factor_universe` 时挂接。
- **逐日白名单**（`filters.StockFilter.__init__`，仅 `index_universe`）。缺方法 → 构造时告警一次；方法在但快照为空 → 构造时告警一次（`index_universe=... 无成分数据，白名单规则不生效`）。

因此缺方法时最多出现两条告警（分属两处，各一次），而有方法无数据时只有 `StockFilter` 侧告警，preload 侧无声。`factor_universe` 没有逐日白名单，只有 preload 路径。

### 10.1 数据卫生检查与空值语义（fail-fast）

与软回退相反，以下问题在 `GenericSQLBackend` 加载期直接 `ValueError`，不会静默跑出错误结果：

- **键列类型探针**：日期键列须为 `YYYYMMDD` 文本、代码键列须为 `VARCHAR`（首次连接时逐表抽样验证）。键列存成 `BIGINT`/`DATE` 会使（交易日, 代码）对齐失真或查询报错，初始化期直接拒绝。
- **重复键检查**：任何面板表的 `(交易日, 代码)` 出现重复即报错（提示前 3 条示例）。重复键会让 FULL OUTER JOIN 多对多爆炸、策略层 `to_dict` 静默丢行。
- **旧格式拒绝**：SQLite 等非 DuckDB 文件在连接期显式报错（DuckDB 的 sqlite_scanner 会静默打开旧库，必须 fail-fast）。
- **空 universe 语义**：`query_bars` 的 `symbols=[]` 返回空面板，`None` 才表示全市场（引擎 preload 用 None；勿用空列表表达“全市场”）。


<a id="semantic-boundaries"></a>
## 11. 数据语义边界

- 新股名单按 `[start-60 日历日, end]` 一次预加载，整个窗口固定排除；不是逐日年龄
  到 60 日自动释放。
- 指数快照先查 start 前 45 日至 end，无结果再查更早历史；逐日取最近一期，但早于首个
  快照时 `StockFilter` 使用首期。需要严格逐时可得性时必须显式核查。
- `get_stock_industries` 只有股票列表参数，没有日期参数；不能宣称历史行业 PIT。
- 分红读取按实施事件归并：有 end_date/ann_date 时，同报告期取最新公告；不同报告期值
  全同取一份，值不同相加并告警；缺上述列时按值去重。重复事件含义仍须回原始公告核对。
- 初始化通过只证明已检查的表结构和键满足条件；单位、公告时点与稀疏数据含义须独立
  抽样验证，方法见[有边界核查](audit_checklist.md#data-probes)。
