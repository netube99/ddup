# ddup 量化研究框架 — 能力全景参考

> 自动生成于 2026-07-27，基于源码深度阅读

---

## 1. Universe / 股票池选择

### 1.1 引擎如何决定交易标的

股票池由三层协作决定：

| 层级 | 配置位置 | 作用 |
|---|---|---|
| **preload 裁剪** | `strategy.get_universe()` | 回测开始前裁剪数据加载范围（减少内存） |
| **日级过滤** | `StockFilter.filter()` | 每日对截面 bars 执行过滤规则 |
| **策略选股** | `strategy.select()` | 从过滤后的候选池中选买卖名单 |

### 1.2 get_universe（preload 裁剪）

- 基类 `Strategy.get_universe()` 默认返回 `None`（全市场）
- 若配置了 `filter_rules.index_universe` 且策略未覆盖此方法，loader **自动生成**默认实现：取所有指数成分的区间并集（`get_index_members` 多指数 UNION），前推 45 天保证回测首日有快照可用
- 自实现时返回 `list[str] | None`（None = 全市场）

### 1.3 benchmark 推导规则

引擎 `Engine.__init__` 中 `benchmark` 的推导优先级：

1. `config["benchmark"]` 显式设置 → 使用该值
2. 未设置 + `filter_rules.index_universe` 恰好 1 个指数 → 使用该指数
3. 未设置 + `index_universe` 多指数或不存在 → 默认 `"000300.SH"`（沪深 300）
4. `config["benchmark"] = ""` 或 `None` → **无基准**（统计输出不含基准对比列）

### 1.4 StockFilter 过滤规则（btcore/filters.py）

```python
class StockFilter:
    def __init__(self, backend, start_date, rules, end_date=None)
    def filter(self, bars: dict, date_str: str) -> dict  # 返回过滤后的 bars
```

**所有 7 条规则：**

| 规则 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `exclude_st` | bool | true | 排 ST。后端需 `get_st_map(from_date)` → `{date: {symbols}}`。当日有记录=当日ST，摘帽次日恢复。缺失时软回退（告警一次）。 |
| `exclude_new_stock` | bool | true | 排近 60 天上市新股。后端需 `get_recent_listings(cutoff_days, as_of)` → `set[symbol]`。缺失时软回退。 |
| `exclude_boards` | list[str] | [] | 排板块。支持：`BJ`、`688`、`300`、`301`。其他代码归为 `MAIN`。 |
| `exclude_industries` | list[str] | [] | 排行业。后端需 `get_stock_industries(symbols)` → `{symbol: industry_name}`。缺失时软回退。 |
| `min_price` | float | 0.0 | 最低收盘价。0=不限制。 |
| `exclude_loss` | bool | true | 排亏损（pe_ttm ≤ 0）。依赖 `pe_ttm` 列——列裁剪下只有**显式声明** `exclude_loss: true` 才会 preload 该列。列缺失时告警一次，规则不生效。 |
| `index_universe` | list[str] | [] | 指数成分白名单（**入场闸**）。后端需 `get_index_members(index_codes, start, end)` → `{snapshot_date: {symbols}}`。多指数取并集，按月频快照取 `≤ 当日` 的最近一期。**持仓被调出指数不强制卖出**。缺失时软回退。 |

### 1.5 板块代码映射

```python
def _get_board(symbol: str) -> str:
    # ".BJ" 后缀 → "BJ"
    # 6位代码：688xxx → "688" / 300xxx → "300" / 301xxx → "301"
    # 其他 → "MAIN"
```

代码格式：`"000001.SZ"` → 取 `"."` 前 6 位判断。

### 1.6 index_weight 表

后端通过 `"index_code"` 和 `"index_member"` 两个位置空声明指数成分数据的位置（必须填在同一张表内，成对）。数据形态为月频快照：`(日期, 指数代码, 成分代码)`。可用指数取决于数据库中实际存储的指数代码（如 `000300.SH` 沪深 300、`000905.SH` 中证 500 等）。

### 1.7 过滤执行顺序

`StockFilter.filter()` 按以下顺序过滤：

1. ST 过滤
2. 新股过滤
3. 板块过滤
4. 行业过滤
5. 指数成分白名单
6. 最低股价过滤
7. 亏损过滤

---

## 2. 策略配置

### 2.1 YAML 完整结构

```yaml
name: my_strategy
strategy: my_package.module:MyStrategy  # 必填：module:Class

config:
  initial_capital: 1000000      # 初始资金（元）
  max_positions: 10             # 最大持仓数
  # --- 可选引擎识别键（全部有默认值）---
  benchmark: "000300.SH"        # 基准代码；""/None = 无基准
  slippage_ticks: 2             # 滑点档数，默认 2（每档 0.01 元）
  commission_rate: 0.00015      # 佣金率，默认万分之 1.5
  min_commission: 5.0           # 最低佣金（元），默认 5 元
  stamp_tax_rate: 0.0005        # 印花税率（仅卖出），默认万分之 5
  transfer_fee_rate: 0.00001    # 过户费率，默认十万分之 1
  order_volume_ratio: 0.1       # 单笔上限 = vol(手) × ratio × 100 股
  execution_price: "close"      # 手动单成交价："open"(默认) | "close"
  condition_slippage_ticks: 2   # 条件单独立滑点（缺省沿用 slippage_ticks）
  quiet_skips: true             # 小资金撮合跳过消息降级为 DEBUG

schedule:                       # 可选：调仓频率（见 2.6）
  frequency: weekly             # daily(默认)|weekly|biweekly|monthly
  weekday: -1                   # weekly/biweekly 用
  monthday: 1                   # monthly 用

factor_specs:                   # 引用 factors/library.yaml 里的名字
  - factor: mom20
    weight: 1.0                 # 加权权重，默认 1.0
    ascending: false            # 得分方向，默认 false（值大优先）

filter_rules:                   # 可选：StockFilter 规则（见 1.4）
  ...
conditions:                     # 可选：声明式条件单（见 2.2）
  ...
risk_rules:                     # 可选：组合级风控（见 2.3）
  ...
```

### 2.2 conditions（条件卖出）

由 `ConditionBuilder` 根据 YAML 声明生成：

| 键 | 类型 | 说明 |
|---|---|---|
| `stop_loss_pct` | float ∈ (0,1) | 止损：成本价 × (1 - pct) → `type=STOP_LOSS` |
| `take_profit_pct` | float ∈ (0,1) | 止盈：成本价 × (1 + pct) → `type=TAKE_PROFIT` |
| `trailing_pct` | float ∈ (0,1) | 移动止盈：持仓最高价 × (1 - pct) → `type=TRAILING_TP` |

策略在 `calc_conditions` 中委托给 `ConditionBuilder.calc()`，返回条件单 dict 列表。

**条件卖出触发规则**：
- `STOP_LOSS` / `TRAILING_TP`：`open ≤ 价格` 按 open 成交；否则 `low ≤ 价格` 按价格成交
- `TAKE_PROFIT`：`open ≥ 价格` 按 open 成交；否则 `high ≥ 价格` 按价格成交

### 2.3 risk_rules（组合级风控）

引擎在 `select()` 之后强制执行，**卖侧永不干预**。

| 键 | 类型 | 说明 |
|---|---|---|
| `max_drawdown` | float ∈ (0,1) | 总权益自峰值回撤 ≥ 该值触发熔断，次日强制清仓 |
| `cooldown_days` | int ≥ 1 | 熔断后冷却 N 个交易日只卖不买，默认 1 |
| `max_position_pct` | float ∈ (0,1) | 单票买入 ≤ 总资产 × pct |
| `max_industry_pct` | float ∈ (0,1) | 单行业 ≤ 总资产 × pct（需后端提供 `get_stock_industries`） |

**行业上限**：入场闸，非持续配平器。只在买入时点把关，持仓上涨自然超限不强制减仓；卖出释放的行业额度当日不回补。名单买单超限直接丢弃，target_value / buy_conditions.value 收缩到行业剩余额度。

### 2.4 执价模式（execution_price）

| 值 | 行为 |
|---|---|
| `"open"` | 默认。T 日信号在 T+1 日**开盘价**成交 |
| `"close"` | T 日信号在 T+1 日**收盘价**成交 |

条件单有独立的盘中触发模型，不受此键影响。

### 2.5 config 全部引擎识别键

除了策略自定义参数外，引擎识别以下键（全部有默认值）：

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `initial_capital` | float | 1_000_000 | 初始资金 |
| `max_positions` | int | 20 | 最大持仓数 |
| `benchmark` | str | 见推导规则 | 基准代码 |
| `slippage_ticks` | int | 2 | 手动单滑点档数（± ticks × 0.01 元） |
| `commission_rate` | float | 0.00015 | 佣金率 |
| `min_commission` | float | 5.0 | 最低佣金（元） |
| `stamp_tax_rate` | float | 0.0005 | 印花税率（卖出） |
| `transfer_fee_rate` | float | 0.00001 | 过户费率 |
| `order_volume_ratio` | float | None | 单笔上限 = `vol(手) × ratio × 100` 股 |
| `execution_price` | str | "open" | 手动单成交价字段 |
| `condition_slippage_ticks` | int | None | 条件单独立滑点，缺省沿用 `slippage_ticks` |
| `quiet_skips` | bool | false | 撮合跳过消息降级为 DEBUG |
| `conditions` | dict | {} | 条件单声明（YAML 顶层键合并进 config） |
| `risk_rules` | dict | {} | 风控声明（YAML 顶层键合并进 config） |

### 2.6 schedule 调仓调度

```yaml
schedule:
  frequency: daily | weekly | biweekly | monthly
  weekday: N           # weekly/biweekly: 每周第 N 个交易日（1起，可负，-1=最后）
  monthday: N          # monthly: 每月第 N 个交易日（1起，可负）
```

- 非调仓日 `select` 返回空买卖名单（持仓不动）
- `calc_conditions` **不受影响**，条件单仍每日生成并触发
- 分组：weekly 按 ISO `(isoyear, isoweek)`，monthly 按 `(year, month)`
- N 超出组内天数则该组不调仓
- 未知键/值在**加载期** `ValueError`

---

## 3. 数据后端能力

### 3.1 DataBackend ABC（3 个抽象方法）

```python
class DataBackend(ABC):
    def query_bars(symbols, start, end, columns=None) -> pd.DataFrame  # MultiIndex (trade_date, symbol)
    def get_calendar(start, end) -> list[str]                           # YYYYMMDD 字符串列表
    def get_dividends_on_date(date_str) -> dict[str, dict[str, float]]  # {symbol: {stk_div, cash_div}}
```

### 3.2 query_bars 列约定

**10 个契约必需列**（缺列 `ValueError: bars 缺必需列`）：

| 列 | 单位/口径 |
|---|---|
| `open` | 裸价（元） |
| `high` | 裸价（元） |
| `low` | 裸价（元） |
| `close` | 裸价（元） |
| `vol` | **手**（1手=100股），`order_volume_ratio` 按此解释 |
| `amount` | 成交额（元） |
| `adj_factor` | 复权因子，**不可缺/NaN** |
| `pre_close` | 昨收（交易所除权调整口径）：除权日 = (前裸收盘 - 现金分红) / (1 + 送转比例) |
| `up_limit` | 涨停价（元），允许个别行 NaN |
| `down_limit` | 跌停价（元），允许个别行 NaN |

**引擎派生列**（不向 backend 请求，自动生成）：
- `open_hfq` / `high_hfq` / `low_hfq` / `close_hfq` = 裸价 × adj_factor
- `pct_chg` = (close - pre_close) / pre_close

**保留列名**（不可作为 extra_fields 键）：`open_hfq` `high_hfq` `low_hfq` `close_hfq` `pct_chg` `idx_ret` `log_mktcap` `industry` `symbol` `trade_date`

**伪列**：引擎运行时附着，不请求：
- `idx_ret`：benchmark hfq_close 日收益（需 benchmark + backend.get_benchmark_bars）
- `log_mktcap`：`np.log(total_mv)`，由 total_mv 派生
- `industry`：行业分组（需 backend.get_stock_industries）

### 3.3 鸭子类型能力（后端自行添加的方法）

不属 ABC，由后端类自行实现，引擎/过滤器/策略按 `hasattr` 鸭子类型调用：

| 方法签名 | 解锁能力 | 缺失行为 |
|---|---|---|
| `get_st_map(from_date) → {date: {symbols}}` | `exclude_st` 过滤 | 软回退（告警一次） |
| `get_recent_listings(cutoff_days, as_of) → set[symbol]` | `exclude_new_stock` 过滤 | 软回退 |
| `get_stock_industries(symbols) → {symbol: industry}` | `exclude_industries`；`industry` 伪列；行业分组算子；`max_industry_pct` | 过滤软回退；因子/风控引用时 preload **报错** |
| `get_index_members(codes, start, end) → {date: {symbols}}` | `index_universe`；`get_universe` 自动生成 | 软回退 |
| `get_benchmark_bars(code, start, end) → DataFrame` | 基准对比；`idx_ret` 伪列 | 统计无基准不报错；`idx_ret` 引用时报错 |

### 3.4 GenericSQLBackend（填表式后端）

零 SQL 接入：99% 场景只需填一份 Python dict。完整声明见 `btcore/generic_sql.py` 头部注释和 `docs/backend_guide.md`。

**表单结构**：
- 通用查询键：`"symbol"` / `"date"`（纯列名）
- 14 个必需位置空：`"表名.字段名"`（10 契约 + calendar_date + dividend × 3）
- 7 个能力空（不填 = 能力关闭）：st_symbol / industry_name / listing_date / index_code / index_member / benchmark_close / benchmark_adj_factor
- `"extra_fields"`：`{列名: "表名.字段名"}` 自选扩展
- `"tables"`：表的特殊说明（filter / filter_sql / symbol / date 覆盖）
- `"benchmark_code"`：默认基准代码

### 3.5 TushareBackend 已声明字段（adapters/tushare.py）

**契约 10 列**：`stk_factor_pro` + `stk_limit` 两张表。
**自选扩展**（约 130+ 列）包括：
- 基本面/估值：pe, pe_ttm, pb, ps, ps_ttm, dv_ratio, dv_ttm, total_share, float_share, free_share, total_mv, circ_mv, turnover_rate, turnover_rate_f, volume_ratio
- 技术指标（bfq 口径全部）：MA(5/10/20/30/60/90/250), EMA(5/10/20/30/60/90/250), MACD, RSI(6/12/24), KDJ, BOLL, BIAS(1/2/3), CCI, ATR, OBV, VR, WR(1), MFI, ASI, DMI(PDI/MDI/ADX/ADXR), CR, PSY, BRAR, DFMA, DPO, EMV, EXPMA, MTM, ROC, TRIX, MASS, KTN, TAQ, BBI, XSII(TD1-4)
- 资金流向：moneyflow（小/中/大/超大单买卖量额 + 净量额）
- 筹码分布：cyq_perf（his_low/high, cost_5/15/50/85/95pct, weight_avg, winner_rate）
- 融资融券：margin_detail（rzye, rqye, rzmre, rqyl 等 8 列）
- 涨跌停：limit_list_d（limit_flag, fd_amount, open_times, limit_times）
- 龙虎榜：top_list（lhb_net_amount, lhb_net_rate, lhb_amount_rate）
- 大宗交易：block_trade（bt_price, bt_amount）
- 东财资金流：moneyflow_dc（2023-09 起）
- 回购：repurchase（事件型）
- 股东增减持：stk_holdertrade（事件型）
- 股东人数：stk_holdernumber（季度频）
- bak_daily：strength, attack, bk_buying, bk_selling
- bak_basic（2026-07-27 新增）：eps, bvps, rev_yoy, profit_yoy, gpr, npr, total_assets, per_undp

---

## 4. 引擎 Preload & 列管理

### 4.1 列裁剪决策（`required_bar_columns()`）

引擎 preload 时**静态推导**所需列，传 `columns=` 给 `query_bars`：

```
主面板列 = REQUIRED_BAR_COLUMNS（10个）
           ∪ strategy.REQUIRED_FIELDS
           ∪ filter_rules 显式依赖（exclude_loss → pe_ttm）
           ∪ 因子闭包基础列（fplan.main_columns - PSEUDO_COLUMNS）
           ↓ _expand_request（派生列展开、伪列移除）
```

**派生列展开**：请求 `close_hfq` → 实际请求 `close` + `adj_factor`

**伪列不请求**：`idx_ret` / `log_mktcap` / `industry` 由引擎运行时附着

### 4.2 REQUIRED_FIELDS

策略在 `select()` 中命令式访问某个列（如 `bar["turnover_rate"]`），**必须**将其声明到 `REQUIRED_FIELDS`：

```python
class MyStrategy(Strategy):
    REQUIRED_FIELDS = [
        "open", "high", "low", "close",
        "vol", "amount", "adj_factor",
        "turnover_rate",   # 我的 select 要用
    ]
```

- 基础列缺失 → KeyError
- extra_fields 列缺失 → get 返回 None（静默错误）

默认值（基类）包含 `open, high, low, close, vol, amount, adj_factor` 7 列。

### 4.3 factor_specs 驱动的列预加载

1. `strategy_loader` 解析 `factor_specs` → 调用 `resolve_closure(names)` 获得传递引用闭包
2. 闭包挂到 `strategy.FACTOR_NODES`
3. 引擎调用 `build_factor_plan(nodes, entry_names)` → 推导两路面板：
   - **主面板**：候选池 × 长窗口 × 基础列（因子列 + where 引用的列）
   - **广度面板**：全市场 × 短窗口 × 窄列（仅坍缩算子节点，如 `mean()` / `group_mean()`）

### 4.4 filter_rules 对 preload 的影响

`filter_required_columns()` 函数只统计**显式开启**的规则：

```python
def filter_required_columns(rules):
    if rules.get("exclude_loss"):
        return {"pe_ttm"}
    return set()
```

- `exclude_st` / `exclude_new_stock` / `exclude_boards` / `exclude_industries` / `min_price` / `index_universe` → **不**影响 bars 列裁剪
- 只有 `exclude_loss: true` → 需要 `pe_ttm`

### 4.5 前视屏蔽机制

三重防护：

1. **因子物化**：preload 后一次性计算，所有 ts（沿时间轴）/ xsec（沿股票轴）算子只用 `≤ 当日` 数据
2. **DataProvider 钳制**：`get_historical_bars()` 的 `end_date` 被钳制到当前模拟日
3. **T+1 撮合**：T 日信号在 T+1 日执行

### 4.6 两路面板物化流程

```python
# 广度面板：全市场 × 短窗口 × 窄列
breadth_df = provider.get_engine_bars(None, end, lookback_start, breadth_columns)

# 主面板：候选池 × 长窗口 × 基础列
bars_df = provider.get_engine_bars(symbols, end, lookback_start, main_columns)

# 物化：广度先算 + 坍缩投影；主面板再算
materialize(bars_df, breadth_df, plan, strategy.FACTOR_NODES)
# 广度面板释放
```

---

## 5. 策略钩子

### 5.1 钩子总览

```python
class Strategy(ABC):
    REQUIRED_FIELDS: ClassVar[list[str]]  # 类变量：声明所需列
    FACTOR_SPECS: ClassVar[list[dict]]     # 类变量：[{name, weight, ascending}]
    FACTOR_NODES: ClassVar[dict | None]    # 由 loader 挂接：因子传递引用闭包
    FILTER_RULES: ClassVar[dict]           # 类变量：过滤规则

    def __init__(self, config, factor_specs=None, filter_rules=None)  # 实例级覆盖

    # ─── 钩子 ───
    def get_universe(provider, start, end) -> list[str] | None  # 【可选】preload 股票池裁剪
    def on_start(provider, first_date, end_date=None) -> None   # 【必选】回测启动一次
    def on_fills(trades, provider) -> None                      # 【可选】每日成交通知（select 之前）
    def select(bars, account_snapshot, provider) -> dict        # 【必选】每日买卖决策
    def calc_conditions(symbol, entry_price, bar, holding_days) -> list[dict]  # 【必选】条件单生成
```

### 5.2 `get_universe(provider, start, end) → list[str] | None`

- 回测开始前调用**一次**，返回候选股票列表以裁剪 preload 数据范围
- 返回 `None` = 全市场
- 配置了 `index_universe` 且未覆盖此方法时，loader 自动生成默认实现

### 5.3 `on_start(provider, first_date, end_date=None)`

- 回测开始前调用**一次**
- 典型用途：初始化 `StockFilter`、`ConditionBuilder`、策略状态、**自定义 condition handler 注册**（必须是进程级全局注册）

### 5.4 `on_fills(trades, provider)`

- 每日 `select` **之前**由引擎调用，告知当日已成交订单
- `trades` 是 `Trade` 列表（trigger 含 MANUAL / TARGET / STOP_LOSS / TAKE_PROFIT / TRAILING_TP / LIMIT_BUY / BREAKOUT_BUY / RISK / CORPORATE）
- 无成交时为空列表；回测首日前也会以空列表调用一次
- 典型用途：止损冷却期管理、按 trigger 区分退出原因、精确重置 trailing 锚点

### 5.5 `select(bars, account_snapshot, provider) → dict`

**参数**：
- `bars`：`{symbol: {open, close, ..., trade_date}}` — 当日截面行情（已过滤）
- `account_snapshot`：`Snapshot(cash, holdings, trades, total_value)`
- `provider`：`DataProvider` — 前视防护门面

**返回协议**（全部通过，详细校验见引擎 `_compute_pending`）：

#### 形式一：买卖名单（与 target_value 互斥）

```python
return {
    "buy": ["000001.SZ", "000002.SZ"],     # 买入名单
    "sell": ["600000.SH"],                 # 卖出名单（默认清仓）
    # 可选附加：
    "buy_weights": {"000001.SZ": 0.06, "000002.SZ": 0.04},  # ∈ (0,1]，键==buy，Σ≤1
    "sell_shares": {"600000.SH": 500},     # 正整数股数，键 ⊆ sell
}
```

- 同日 `buy ∩ sell` 必须为空
- 买入默认等权分配（total_value / max_positions）
- 新买入受 `max_positions` 硬上限

#### 形式二：目标仓位（与买卖名单互斥）

```python
return {
    "buy": [],
    "sell": [],
    "target_value": {
        "000001.SZ": 100000.0,    # 目标市值
        "000002.SZ": 50000.0,
        "old_holding.SH": 0.0,    # 0 = 清仓
    },
}
```

- 次日按目标市值调仓（高于目标卖、低于目标买），trigger="TARGET"
- 未列出的持仓不处理
- 仍受 `max_positions` 约束

#### 形式三：条件买入（可附加在形式一上，与 target_value 互斥）

```python
return {
    "buy": [],
    "sell": [],
    "buy_conditions": [
        {"symbol": "000001.SZ", "type": "LIMIT_BUY", "price": 9.80, "value": 50000.0},
        {"symbol": "000002.SZ", "type": "BREAKOUT_BUY", "price": 15.00, "shares": 3000},
    ],
}
```

- `type` 内置：`LIMIT_BUY`（open ≤ price 按 open；low ≤ price 按 price）、`BREAKOUT_BUY`（open ≥ price 按 open；high ≥ price 按 price）
- `value` / `shares` **恰填一个**
- T 日声明、T+1 盘中触发、**单日有效**（未触发即失效，需每日重新声明）
- 撮合顺序在手动单 + 条件卖单之后（吃到卖出释放的现金）

**冲突校验总表：**

| 组合 | 结果 |
|---|---|
| buy + sell 交集非空 | `同日买卖冲突` |
| buy/sell + target_value | `target_value 与 buy/sell 名单互斥` |
| sell + buy_conditions 交集非空 | `同日卖出与条件买入冲突` |
| buy + buy_conditions 交集非空 | `buy 名单与条件买入重复` |
| target_value + buy_conditions | `target_value 与 buy_conditions 互斥` |

### 5.6 `calc_conditions(symbol, entry_price, bar, holding_days) → list[dict]`

- 每日对每个持仓调用一次
- 返回条件卖单列表：`[{"type": "STOP_LOSS", "price": 9.20}, ...]`
- 典型实现委托给 `ConditionBuilder.calc()`

### 5.7 `provider` 对象（DataProvider）

```python
class DataProvider:
    backend          # 数据后端对象（鸭子类型直调）
    # 引擎用
    get_engine_bars(symbols, trade_date, lookback_start, columns)  # 含当日
    # 策略用
    attach_bars(bars_df)                    # 【内部接线】引擎注入全量数据
    get_historical_bars(symbols, end_date, lookback_days=365)  # 不含当日，被钳制
    # 透传
    get_calendar(start, end) → list[str]
    get_dividends_on_date(date_str) → dict
```

### 5.8 `account_snapshot`（Snapshot 具名元组）

```python
@dataclass
class Snapshot:
    cash: float               # 当日结算后现金
    holdings: dict[str, Holding]  # 深拷贝只读副本（改动不影响引擎状态）
    trades: list[Trade]       # 当日成交列表（同 on_fills 收到的）
    total_value: float        # 当日结算后总权益（现金 + 持仓 × last_price）

@dataclass
class Holding:
    symbol: str
    shares: int
    entry_date: str
    entry_price: float         # 建仓均价
    cost: float                # 持仓成本
    last_price: float          # 最新收盘价
    holding_days: int
    conditions: list[dict]     # 当前条件单
    locked: bool               # T+1 锁（买入当日 True，次日解锁）
```

### 5.9 条件单类型注册

```python
from btcore.match.conditions import register_condition_handler, register_buy_condition_handler

# 必须在 on_start 中注册（进程级全局状态），不能在类级别
register_condition_handler("MY_EXIT", my_exit_handler)     # 条件卖出
register_buy_condition_handler("MY_ENTRY", my_entry_handler)  # 条件买入
```

内置类型：`STOP_LOSS` / `TAKE_PROFIT` / `TRAILING_TP`（卖出）、`LIMIT_BUY` / `BREAKOUT_BUY`（买入）。

### 5.10 ConditionBuilder

```python
class ConditionBuilder:
    def __init__(self, rules: dict)
    def calc(symbol, entry_price, bar, holding_days) -> list[dict]  # 生成条件单
    def prune(live_symbols)  # 清理已平仓标的的 trailing 状态
```

### 5.11 辅助工具（btcore.strategy_tools）

```python
bars_to_df(bars: dict) -> pd.DataFrame                         # 截面 dict→DataFrame
eval_factor_specs(df, factor_specs) -> (factor_df, score)       # 读物化因子列 + 加权得分
parse_schedule(raw: dict) -> dict                               # 校验 schedule
wrap_strategy(strategy, rule) -> Strategy                       # 调仓调度包装
```

---

## 附录 A：关键设计原则

### 软回退 vs Fail-Fast 分界

| 场景 | 行为 |
|---|---|
| ST 表/行业/指数成分等鸭子类型能力缺失 | **软回退**：告警一次，对应规则不生效，策略继续运行 |
| 伪列引用（idx_ret/industry/log_mktcap）无后端支持 | **Fail-fast**：preload 直接报错（策略明确依赖） |
| 必需列缺失 | **Fail-fast**：`ValueError: bars 缺必需列` |
| 因子名不存在 / 因子引用环 | **Fail-fast**：加载期报错 |
| 表单引用表/列不存在 | **Fail-fast**：初始化期校验报错 |
| 条件单 type 未注册 | **Fail-fast**：`_compute_pending` 阶段 `ValueError` |

### 价格体系：裸价 vs 后复权

- **撮合、成本、估值**：使用裸价（`open` / `close`）
- **因子计算、排名**：使用后复权（`*_hfq`）
- 两者**不可混用**：裸价做排序会因除权除息产生虚假信号

### T+1 约束

- 买入当日 `Holding.locked = True`，次日解锁
- 锁定期间条件单跳过该持仓（不会当天买入当天止损卖出）
- 涨停不买（fill_price ≥ up_limit 或涨跌停不可判定）、跌停不卖、price NaN 跳过

### 多 run 累积

同一 db_path 多次 `engine.run()`，历史 run 按 `run_id` 隔离保留。run 中抛异常时 status 改写为 `failed`。

---

## 附录 B：回测结果库 Schema

### runs
| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER PK AUTOINCREMENT | |
| `created_at` | TEXT | 时间戳 |
| `strategy` | TEXT | 策略类名 |
| `start_date` / `end_date` | TEXT | 区间 |
| `initial_capital` | REAL | |
| `config_json` | TEXT | 策略完整配置 JSON |
| `status` | TEXT | running / completed / failed |
| `stats_json` | TEXT | statistics JSON |

### account_daily
| 列 | 类型 |
|---|---|---|
| `run_id` | INTEGER |
| `date` | TEXT |
| `cash` | REAL |
| `total_value` | REAL |
| `daily_pnl` | REAL |
| `cumulative_pnl` | REAL |
| `initial_capital` | REAL |
| `n_holdings` | INTEGER |

### trade_log
| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER | |
| `date` | TEXT | |
| `symbol` | TEXT | |
| `side` | TEXT | BUY / SELL / DIV |
| `trigger` | TEXT | MANUAL / TARGET / STOP_LOSS / TAKE_PROFIT / TRAILING_TP / LIMIT_BUY / BREAKOUT_BUY / RISK / CORPORATE |
| `price` | REAL | 成交价（已含滑点） |
| `shares` | INTEGER | |
| `turnover` | REAL | 成交额 |
| `commission` / `stamp_tax` / `transfer_fee` | REAL | 费用明细 |
| `slippage_amount` | REAL | 滑点金额 |
| `net_amount` | REAL | 净现金流 |
| `reason` | TEXT | 备注 |

## 附录 C：CLI 用法

```bash
python scripts/run.py <策略.yaml> \
    --start YYYYMMDD \
    --end YYYYMMDD \
    [--capital 初始资金] \
    [--out 结果库路径] \
    [--report 报告.html] \
    [--no-report]

# 离线报告
python scripts/report.py result.db [--run-id N] --out report.html

# 多 run 对比
python scripts/compare.py result.db [--runs 1,2,3] [--html compare.html]
```

## 附录 D：程序式 API

```python
from adapters.tushare import TushareBackend
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy, build_strategy

# YAML 路径
strategy = load_strategy("strategies/examples/topk_momentum/config.yaml")
provider = DataProvider(TushareBackend("/path/to/market.db"))
engine = Engine(strategy, provider, initial_capital=2_000_000, db_path="result.db")
result = engine.run("20240101", "20240630")

# 程序化构建（遍历因子组合）
strategy = build_strategy(
    TopKMomentum,
    config={"initial_capital": 1_000_000, "max_positions": 10, "top_k": 5},
    factor_specs=[{"name": "mom20", "weight": 1.0, "ascending": False}],
    filter_rules={"exclude_st": True, "min_price": 3.0},
    schedule={"frequency": "weekly", "weekday": 1},
)

# run 返回值
result["account_daily"]   # 每日账户快照 DataFrame
result["trade_log"]       # 完整成交记录 DataFrame
result["statistics"]      # 绩效指标 dict
result["benchmark_nav"]   # 基准净值序列
result["benchmark_code"]  # 基准代码
```
