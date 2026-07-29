# 策略设计指南

ddup 的策略系统由两部分构成：**YAML 配置**声明因子引用、过滤规则、条件单、风控参数和调度规则；**Python 策略类**实现 `on_start` / `select` / `calc_conditions` 三个核心方法。引擎负责 preload、因子物化、撮合、风控执行和结果落库——策略代码只描述买卖决策。

本文从快速开始出发，逐级展示从简单轮动到状态机多模型策略的全部能力。

---

## 1. 快速开始

最小可运行策略：YAML 声明 + Python 逻辑，两个文件。

**config.yaml**：

```yaml
strategy: strategies.my_strategy:MyStrategy

factor_specs:
  - factor: mom20              # 引用 factors/library.yaml 中定义的因子名
    weight: 1.0
    ascending: false           # false=值越大越好

config:
  max_positions: 10
  top_k: 5                     # 持有前 5 只（自定义键，策略代码中 self.config.get("top_k") 读取）

filter_rules:
  exclude_st: true
  exclude_new_stock: true
  min_price: 3.0

conditions:
  stop_loss_pct: 0.08          # 跌 8% 止损
```

**strategy.py**：

```python
from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

class MyStrategy(Strategy):
    def on_start(self, provider, first_date, end_date=None):
        self._top_k = int(self.config.get("top_k", 5))
        self._filter = StockFilter(provider.backend, first_date, self.FILTER_RULES, end_date=end_date)
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

    def select(self, bars, account_snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        target = set(score.sort_values(ascending=False).head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        return {"buy": sorted(target - current), "sell": sorted(current - target)}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return self._cond.calc(symbol, entry_price, bar, holding_days)
```

**运行**：

```bash
python scripts/run.py strategies/my_strategy/config.yaml --start 20240101 --end 20240630
```

这就是一个完整策略的全部代码。以下逐层展开。

---

## 2. 策略架构总览

### 2.1 引擎与策略的交互生命周期

```
on_start (一次)
  │
  └─ [每日循环] ─────────────────────────────────────────────┐
       ├─ 公司行为调整 (分红/送转)                            │
       ├─ 撮合昨日信号 (手动买卖 + 条件卖出 + 条件买入)       │
       ├─ NAV 结算                                           │
       ├─ on_fills(trades)       ← 感知当日已撮合成交        │
       ├─ on_tick(bars, snapshot) ← 每日状态维护              │
       ├─ select(bars, snapshot) → {buy, sell, ...}          │
       ├─ calc_conditions() × N   ← 每个持仓生成条件单        │
       ├─ 风控裁剪 (DrawdownBreaker + apply_risk_rules)      │
       └─ 生成明日 pending_actions ──────────────────────────┘
```

> `select` 和风控生成的是**下一交易日**将执行的买卖信号，当日不成交——这是 T+1 机制的源头。当日撮合的是前一日 `select` 输出的 pending_actions。

`on_fills` 和 `on_tick` 是可选的。`on_tick` 每日运行（即使非调仓日 schedule 包装器拦截了 `select`）；`calc_conditions` 同样每日运行。

> **设计契约（策略作者须知）**
>
> 以下两条是引擎运行的公理，任何策略代码都不能绕过：
>
> - **价格体系**：撮合、成本、估值使用裸价（`open` / `close`），因子计算、排名使用后复权（`*_hfq`）。混用会导致除权除息产生虚假信号——例如用裸价做排名，除权日股价跳空会被误判为"暴跌"。
> - **软回退与 Fail-Fast**：可选能力缺失（ST 表、行业表、指数成分表）→ 引擎告警后继续运行，对应规则不生效。明确声明的依赖缺失（因子引用的伪列无后端支持、必需列缺失、因子名不存在）→ 加载或 preload 阶段直接报错，不产生静默错误结果。

### 2.2 策略作者职责一览

| 职责 | 方式 | 说明 |
|------|------|------|
| 必须实现 | `on_start(provider, first_date, end_date)` | 初始化过滤器、状态字典、注册自定义 handler |
| 必须实现 | `select(bars, snapshot, provider)` → dict | 每日买卖决策 |
| 必须实现 | `calc_conditions(symbol, entry_price, bar, holding_days)` → list[dict] | 每个持仓每日的条件单 |
| 可选实现 | `on_fills(trades, provider)` | 感知成交 → 冷却期、状态跟踪 |
| 可选实现 | `on_tick(bars, snapshot, provider)` | 每日状态维护（市场检测、冷却递减） |
| 可选覆盖 | `get_universe(provider, start, end)` → list[str] \| None | 自定义交易域 |
| 可选覆盖 | `get_factor_universe(provider, start, end)` → list[str] \| None | 自定义因子计算域 |
| 声明式 | `REQUIRED_FIELDS: list[str]` | 声明 `select()` 中命令式访问的列 |
| 声明式 | `FACTOR_SPECS: list[dict]` | 因子引用列表（YAML 或类变量） |
| 声明式 | `FILTER_RULES: dict` | 过滤规则默认值（YAML 或类变量） |

### 2.3 引擎提供的工具

| 工具 | 位置 | 用途 |
|------|------|------|
| `bars_to_df(bars)` | `btcore.strategy_tools` | dict-of-dicts → symbol 索引 DataFrame |
| `eval_factor_specs(df, specs)` → (DataFrame, Series) | `btcore.strategy_tools` | 读物化因子列 → 合成得分 (0~1) |
| `ConditionBuilder(rules)` | `btcore.strategy_tools` | 声明式条件单构建 + trailing 状态跟踪 |
| `StockFilter(backend, start, rules)` | `btcore.filters` | 截面多规则过滤 |
| `register_condition_handler(type, handler)` | `btcore.match.conditions` | 注册自定义离场条件 |
| `register_buy_condition_handler(type, handler)` | `btcore.match.conditions` | 注册自定义买入条件 |

> 含完整代码签名的速查表见 §10.6（引擎工具函数一览）。

---

## 3. 策略 Python 接口参考

### 3.1 Strategy 基类

```python
from btcore.strategy import Strategy

class MyStrategy(Strategy):
    # 类变量 —— 声明式默认值，YAML 可覆盖
    REQUIRED_FIELDS: list[str] = ["open", "high", "low", "close", "vol", "adj_factor"]
    FACTOR_SPECS: list[dict] = []
    FACTOR_NODES: dict | None = None   # 由 loader 挂接，用户不设置
    FILTER_RULES: dict = {}
```

构造函数签名 `__init__(self, config, factor_specs=None, filter_rules=None)`。实例化后：
- `self.config` — YAML `config` 节的完整 dict
- `self.FACTOR_SPECS` — `[{name, weight, ascending}]` 格式的因子引用列表
- `self.FILTER_RULES` — 过滤规则 dict

### 3.2 `on_start(self, provider, first_date, end_date=None)`

**必须实现。** 回测开始前调用一次。在此初始化所有策略状态：

- 创建 `StockFilter`：`StockFilter(provider.backend, first_date, self.FILTER_RULES, end_date=end_date)`
- 创建 `ConditionBuilder`：`ConditionBuilder(self.config.get("conditions", {}))`
- 注册自定义条件单 handler（见 §5.2.3）
- 解析自定义 config 参数：`self._top_k = int(self.config.get("top_k", 5))`
- 初始化状态字典：冷却期 map、持仓跟踪 dict、市场状态变量
- 可在此做一次性计算（预加载历史数据、初始化模型参数等）

### 3.3 `select(self, bars, account_snapshot, provider) → dict`

**必须实现。** 每个交易日调用。参数：

- `bars`: `dict[str, dict]` — 当日截面数据，键为 symbol，值为包含 OHLCV + 因子列 + 扩展字段的 dict。引擎保证 `trade_date` 字段存在。
- `account_snapshot`: `Snapshot` 对象，属性包括：
  - `cash` — 可用现金
  - `holdings` — `dict[str, Holding]`，当前持仓的深拷贝（修改不影响引擎状态）
  - `trades` — 当日已执行的成交列表 `list[Trade]`（与 `on_fills` 收到的同一份数据）
  - `total_value` — 总资产（现金 + 持仓市值）
- `provider`: `DataProvider` 对象，提供 `get_historical_bars()`、`get_benchmark_returns()` 等方法

**返回值**是一个 dict，支持以下键：

| 键 | 类型 | 语义 |
|---|---|---|
| `buy` | `list[str]` | 买入名单。空列表 = 无买入。 |
| `sell` | `list[str]` | 卖出名单（全部清仓）。空列表 = 无卖出。 |
| `buy_weights` | `dict[str, float] \| None` | 每只买入的资金权重，和 ≤ 1，每项 ∈ (0, 1]。引擎按 `total_value * weight` 分配资金。键必须精确匹配 `buy` 列表。`None` = 等权买入。 |
| `sell_shares` | `dict[str, int] \| None` | 部分减仓股数。键必须是 `sell` 列表的子集，值为正整数。不在 `sell` 中的 symbol 会被引擎忽略。 |
| `buy_conditions` | `list[dict] \| None` | T+1 日盘中条件买单列表。每条 dict 格式见 §5.2.2。与 `target_value` 同一天互斥。 |
| `target_value` | `dict[str, float] \| None` | 每只股票的目标市值。引擎自动计算买卖差额。`0` = 清仓。未出现的 symbol 不动。与 `buy`/`sell`/`buy_conditions` 同一天互斥。 |

**冲突校验**（引擎自动执行，违规即报错）：

- `buy` 与 `sell` 有交集 → `ValueError`
- `target_value` 与 `buy`/`sell` 同时非空 → `ValueError`
- `buy_weights` 的键与 `buy` 列表不一致 → `ValueError`
- `sell_shares` 的键不在 `sell` 列表中 → `ValueError`

### 3.4 `calc_conditions(self, symbol, entry_price, bar, holding_days) → list[dict]`

**必须实现。** 引擎对**每个持仓每日**调用。返回条件单 dict 列表；空列表 = 该持仓无条件单。

参数：
- `symbol` — 股票代码
- `entry_price` — 该持仓的入场均价（公司行为调整后）
- `bar` — 该 symbol 当日的 bar 数据 dict
- `holding_days` — 持仓天数（含本日）

返回的每条条件单 dict 至少包含 `type`（str）和 `price`（float 或 None）。引擎按列表顺序评估，**首条触发生效**（后续条件不再检查）。`price` 为 `None` 时 handler 自行计算。

简便用法：委托给 `ConditionBuilder.calc()`（见 §5.2.1）。

### 3.5 `on_fills(self, trades, provider)`

**可选 hook。** 引擎在 `select` 之前调用，传入当日实际成交的 `Trade` 对象列表。无成交时为空列表。

`Trade` 属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `date` | `str` | 成交日期 (YYYYMMDD) |
| `symbol` | `str` | 股票代码 |
| `side` | `str` | `"BUY"` 或 `"SELL"` |
| `trigger` | `str` | 触发类型（见 §10.4） |
| `price` | `float` | 成交价 |
| `shares` | `int` | 成交股数 |
| `turnover` | `float` | 成交金额 |
| `commission` | `float` | 佣金 |
| `stamp_tax` | `float` | 印花税 |
| `transfer_fee` | `float` | 过户费 |
| `slippage_amount` | `float` | 滑点金额 |
| `net_amount` | `float` | 净现金流 |
| `reason` | `str` | 备注 |

典型用途：感知条件单止损/止盈退出 → 对标的施加冷却期；记录入场价、最高价等持仓状态；重置 trailing 锚点。

### 3.6 `on_tick(self, bars, snapshot, provider)`

**可选 hook。** 每日调用——**即使非调仓日 schedule 包装器拦截了 `select`，`on_tick` 仍然运行**。在 `on_fills` 之后、`select` 之前调用。

典型用途：
- 冷却期递减计数
- 市场状态机推进（广度检测 → 状态切换）
- 逐仓最高价跟踪
- `ConditionBuilder.prune()` 清理已平仓标的的 trailing 状态
- 任何需要每日更新的策略内部状态

### 3.7 `get_universe(self, provider, start, end) → list[str] | None`

**可选覆盖。** 返回策略需要的股票符号列表。基类返回 `None`（全市场）。引擎 preload 阶段调用，用于裁剪数据加载范围。

如果 YAML `filter_rules` 中配置了 `index_universe` 且策略**未覆盖**此方法，loader 会自动生成一个实现，返回多指数成分股的并集。手动覆盖后 `index_universe` 配置不再自动生效。

### 3.8 `get_factor_universe(self, provider, start, end) → list[str] | None`

**可选覆盖。** 返回因子计算所需的股票符号列表，可以宽于交易域（`get_universe`）。基类返回 `None`（因子计算域 = 交易域）。

如果 YAML `filter_rules` 中配置了 `factor_universe` 且未覆盖此方法，loader 自动生成。

### 3.9 类变量 `REQUIRED_FIELDS`

```python
REQUIRED_FIELDS: list[str] = ["open", "high", "low", "close", "vol", "adj_factor"]
```

声明 `select()` 中**命令式访问**的 bar 列。引擎 preload 做列裁剪时，没有在此声明且不在 FACTOR_NODES 中的列会被裁剪掉。不需要声明：
- 因子系统自动覆盖的列（`FACTOR_NODES` 和 `FACTOR_SPECS` 引用的因子列）
- 引擎默认基础列（`open`/`high`/`low`/`close`/`vol`/`adj_factor` 永不裁剪）

例如策略的 `select()` 中访问了 `bar["turnover_rate"]` 或 `bar["pe_ttm"]`，必须在此声明。

---

## 4. YAML 配置完整参考

### 4.1 顶层键

| 键 | 必需 | 类型 | 说明 |
|---|---|---|---|
| `strategy` | **是** | `str` | `"module.path:ClassName"`，指向 `Strategy` 子类 |
| `name` | 否 | `str` | 策略名称（仅标注用） |
| `config` | 否 | `dict` | 策略配置参数 |
| `factor_specs` | 否 | `list[dict]` | 因子引用列表 |
| `filter_rules` | 否 | `dict` | 股票过滤规则 |
| `conditions` | 否 | `dict` | 声明式离场条件单 |
| `risk_rules` | 否 | `dict` | 组合风控规则 |
| `schedule` | 否 | `dict` | 调仓频率调度 |
| `factor_library` | 否 | `str` | 自定义因子库路径，默认 `factors/library.yaml` |

`conditions` 和 `risk_rules` 由 loader 合并入 `self.config["conditions"]` 和 `self.config["risk_rules"]`。

### 4.2 `config` 引擎识别键

以下键被引擎直接消费，均有默认值，全部可选：

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `initial_capital` | `float` | `1000000` | 初始资金（元） |
| `max_positions` | `int` | `20` | 最大持仓数（硬上限） |
| `slippage_ticks` | `int` | `2` | 手动买卖滑点 tick 数（1 tick = 0.01 元） |
| `condition_slippage_ticks` | `int \| None` | `None` | 条件单独立滑点 tick 数；`None` 时沿用 `slippage_ticks` |
| `execution_price` | `str` | `"open"` | 手动订单成交价：`"open"`（次日开盘价）或 `"close"`（次日收盘价） |
| `commission_rate` | `float` | `0.00015` | 佣金费率（万1.5） |
| `min_commission` | `float` | `5.0` | 最低佣金（元） |
| `stamp_tax_rate` | `float` | `0.0005` | 印花税费率（仅卖出收取） |
| `transfer_fee_rate` | `float` | `0.00001` | 过户费费率 |
| `benchmark` | `str \| None` | 自动推导 | 基准指数代码（如 `"000300.SH"`）。单指数 `index_universe` 时自动取第一个；否则默认沪深300 |
| `quiet_skips` | `bool` | `False` | `True` 时将跳过的警告降级为 DEBUG |
| `order_volume_ratio` | `float \| None` | `None` | 单笔订单股数 ≤ `成交量(手) × ratio × 100`；`None` 时不限制 |

**自定义键**：任何不在此列表的键（如 `top_k`、`cooldown_days`、`hunt_count`）策略代码通过 `self.config.get("key")` 自由读取，引擎不干预。

### 4.3 `factor_specs`

```yaml
factor_specs:
  - factor: mom20          # 因子库中的名字（必填）
    weight: 1.0            # 合成权重，默认 1.0
    ascending: false       # false=值越大排名越靠前（默认）
  - factor: vol_z
    weight: 0.4
    ascending: true        # true=值越小排名越靠前（低波异象）
```

每个条目被 loader 规范化为 `{name, weight, ascending}`，挂接到 `strategy.FACTOR_SPECS`。

### 4.4 `filter_rules`

| 键 | 类型 | 默认 | 需要后端能力 | 说明 |
|---|---|---|---|---|
| `exclude_st` | `bool` | `True` | `get_st_map` / `st_symbol` | 排除 ST 股票 |
| `exclude_new_stock` | `bool` | `True` | `listing_date` | 排除上市 60 日内的新股 |
| `exclude_loss` | `bool` | `True` | —（需 `pe_ttm` 列） | 排除 `pe_ttm <= 0` 的亏损股 |
| `exclude_boards` | `list[str]` | `[]` | — | 排除板块：`"BJ"`（北交所）、`"688"`（科创板）、`"300"`/`"301"`（创业板） |
| `exclude_industries` | `list[str]` | `[]` | `industry_name` | 排除指定行业名的股票 |
| `min_price` | `float` | `0.0` | — | 最低收盘价过滤 |
| `index_universe` | `list[str]` | — | `index_code` + `index_member` | 仅交易指数成分股（多指数并集）。不填 = 全市场 |
| `factor_universe` | `list[str]` | — | `index_code` + `index_member` | 因子计算域（可宽于交易域）。不填 = 沿用交易域 |

`exclude_loss` 需显式声明才会触发 preload 加载 `pe_ttm` 列。未声明时即使后端有 `pe_ttm` 数据，引擎也不加载它。

### 4.5 `conditions` — 声明式离场条件

```yaml
conditions:
  stop_loss_pct: 0.06    # 跌 6% 止损 → STOP_LOSS at entry_price × (1 - 0.06)
  take_profit_pct: 0.25  # 涨 25% 止盈 → TAKE_PROFIT at entry_price × (1 + 0.25)
  trailing_pct: 0.08     # 从最高点回撤 8% 止盈 → TRAILING_TP at highest × (1 - 0.08)
```

所有值必须是 `(0, 1)` 内的 float。这三项由 `ConditionBuilder` 翻译为条件单 dict，策略在 `calc_conditions` 中通过 `self._cond.calc()` 委托。

### 4.6 `risk_rules` — 组合风控

```yaml
risk_rules:
  max_drawdown: 0.12         # 总权益自峰值回撤 ≥ 12% 触发熔断
  cooldown_days: 5           # 熔断后冷却 5 个交易日（在此期间强制只卖不买）
  max_position_pct: 0.10     # 单票买入金额 ≤ 总资产的 10%
  max_industry_pct: 0.30     # 单行业总暴露 ≤ 总资产的 30%（需 industry_name 后端能力）
```

- `max_drawdown` 触发后进入冷却期：所有持仓以 `trigger="RISK"` 强制清仓，买侧彻底关闭。
- `cooldown_days` 到期的下一天重置峰值，允许策略重新出发。出现 `max_drawdown` 时 `cooldown_days` 默认 1。
- `max_industry_pct` 是"入场闸"而非"持续配平器"——只在买入时把关，持仓自然上涨超出上限不强制减仓。

### 4.7 `schedule` — 调仓调度

```yaml
schedule:
  frequency: weekly           # daily (默认) | weekly | biweekly | monthly
  weekday: -1                 # 每周最后一个交易日（1 起，负数倒数）
```

```yaml
schedule:
  frequency: biweekly
  weekday: 1                  # 每两周第一个交易日
```

```yaml
schedule:
  frequency: monthly
  monthday: 1                 # 每月第一个交易日
```

- `frequency: daily` — 每个交易日调仓（缺省行为）
- `frequency: weekly` / `biweekly` — `weekday` 指定周内第 N 个交易日。负数从周尾倒数，超出范围则该周不调仓
- `frequency: monthly` — `monthday` 指定月内第 N 个交易日。负数同理
- 非调仓日 `select()` 被包装器拦截返回空名单；`on_tick` 和 `calc_conditions` 不受影响

### 4.8 `factor_library`

```yaml
factor_library: factors.yaml   # 相对于策略 YAML 所在目录
```

指向自定义因子库 YAML。不填时使用 `factors/library.yaml`。详见 `docs/factor_library.md`。

---

## 5. 核心机制详解

### 5.1 select 返回协议

引擎从 `select()` 的返回值中提取六种可能的键，组合规则如下：

**模式一：买卖名单模式**（最常用）

返回 `buy` 和/或 `sell` 列表。可选附带 `buy_weights`、`sell_shares`、`buy_conditions`。

- `buy` 名单用等权或加权分配资金买入
- `sell` 名单全部清仓（除非提供了 `sell_shares` 部分减仓）
- `buy_conditions` 可与 `buy` 名单并存（互不冲突）

**模式二：目标市值模式**

返回 `target_value`。引擎自动计算每只股票的目标市值与当前市值的差额，卖出超配部分、买入缺额部分（trigger = `"TARGET"`）。

- 目标市值 = 0 → 清仓
- 未出现在 dict 中的持仓 → 不动
- 与 `buy`/`sell`/`buy_conditions` 同一天互斥

**空返回**：`{"buy": [], "sell": []}` 是完全合法的——表示今日不做任何操作。引擎静默处理。

### 5.2 条件单系统

#### 5.2.1 声明式离场条件（ConditionBuilder）

```python
self._cond = ConditionBuilder(self.config.get("conditions", {}))
# 在 calc_conditions 中委托：
return self._cond.calc(symbol, entry_price, bar, holding_days)
```

`ConditionBuilder` 自动将 YAML 声明的 `stop_loss_pct` / `take_profit_pct` / `trailing_pct` 翻译为对应的条件单 dict。`TRAILING_TP` 的移动止盈最高价由 `ConditionBuilder` 内部跟踪，通过 `prune()` 清理已平仓标的。

**触发规则**：

| 条件 | 方向 | open 越过 | 否则 low/high 越过 | 否则 |
|------|------|-----------|-------------------|------|
| `STOP_LOSS` | 卖出 | `open <= price → fill at open` | `low <= price → fill at price` | 不触发 |
| `TAKE_PROFIT` | 卖出 | `open >= price → fill at open` | `high >= price → fill at price` | 不触发 |
| `TRAILING_TP` | 卖出 | 同 STOP_LOSS | 同 STOP_LOSS | 不触发 |

条件单按**列表顺序**评估，首条触发生效即 break。一般顺序：止损 → 止盈 → 移动止盈。

#### 5.2.2 条件买入

`select()` 返回 `buy_conditions` 列表，每条 dict 格式：

```python
{
    "symbol": "000001.SZ",
    "type": "LIMIT_BUY",          # 或 "BREAKOUT_BUY"
    "price": 12.50,               # 触发价（必须 > 0）
    "value": 20000.0,             # 买入金额（与 shares 二选一）
    # 或 "shares": 2000,         # 精确股数
}
```

- `value` 和 `shares` 必须提供其一且仅其一
- `value` 模式下引擎自动取整到 100 股（1 手）的整数倍

**内置类型**：

| 类型 | 触发逻辑 |
|------|----------|
| `LIMIT_BUY` | 限价回踩：`open <= price → fill at open`；否则 `low <= price → fill at price` |
| `BREAKOUT_BUY` | 突破追涨：`open >= price → fill at open`；否则 `high >= price → fill at price` |

**引擎自动执行的约束**：
1. 已持有的 symbol 跳过
2. `max_positions` 硬上限——达到上限后停止处理后续订单
3. bar 缺失 → 跳过
4. 涨停（`price >= up_limit`）→ 跳过
5. 成交量为零 → 跳过
6. `order_volume_ratio` 成交量 cap 应用
7. 现金不足 → `shrink_to_affordable` 缩股至可负担
8. 缩股后 < 100 股 → 跳过
9. T+1 锁定（买入当天不可卖出）

条件买单的滑点使用 `condition_slippage_ticks`（独立于手动买卖的 `slippage_ticks`）。

#### 5.2.3 自定义条件单 handler

**离场**：

```python
from btcore.match.conditions import register_condition_handler

def my_handler(holding, cond, bar):
    """
    holding: Holding 对象（entry_price, holding_days, shares 等）
    cond:   条件单 dict（策略在 calc_conditions 中传入的自定义键）
    bar:    当日 bar 数据

    返回: (executed: bool, fill_price: float, log_params: dict)
    """
    ...

# 在 on_start 中注册（进程级全局）
register_condition_handler("MY_CONDITION", my_handler)
```

然后在 `calc_conditions` 中返回含此 type 的条件单：

```python
def calc_conditions(self, symbol, entry_price, bar, holding_days):
    conds = self._cond.calc(symbol, entry_price, bar, holding_days)
    conds.append({"type": "MY_CONDITION", "price": None, "custom_param": value})
    return conds
```

**入场**：

```python
from btcore.match.conditions import register_buy_condition_handler

def my_buy_handler(order, bar):
    """
    order: buy_conditions 中的 dict（symbol, type, price, value/shares + 自定义键）
    bar:   当日 bar 数据

    返回: (executed: bool, fill_price: float, log_params: dict)
    """
    ...

register_buy_condition_handler("MY_BUY", my_buy_handler)
```

### 5.3 风控执行顺序

每日流水线：

```
select() → DrawdownBreaker.update() → DrawdownBreaker.tick()
  → 若熔断: 强制 sell = 全部持仓, buy = []
  → 否则: apply_risk_rules() 裁剪买侧
  → 撮合
```

**熔断详细行为**：
- 总资产自峰值回撤 ≥ `max_drawdown` → 触发
- 触发后**当天**强制清仓（trigger = `"RISK"`），之后冷却 `cooldown_days` 个交易日
- 冷却期间所有 buy 被抑制（包括条件买单）
- 冷却到期后峰值重置，允许策略重新出发

**`max_position_pct` 裁剪**：target_value 中的目标市值、buy_conditions 中的 value、buy_weights 的分配金额均被 cap 到 `total_value × pct`。

**`max_industry_pct` 裁剪**：买入时检查该股票所在行业的总暴露（持仓市值 + 新买单金额）是否超过上限。超出则丢弃该买单或缩减 target_value。卖出释放的行业余量不在当日回补。

### 5.4 调度器机制

`wrap_strategy` 在 `on_start` 末尾用 `provider.get_calendar()` 预计算调仓日集合。非调仓日的 `select` 被替换为返回 `{"buy": [], "sell": []}`。不影响 `on_tick` 和 `calc_conditions`。

### 5.5 撮合与执行

**手动买卖**（`buy`/`sell` 名单）：
- 执行价 = `execution_price` 指定的开盘价或收盘价
- 等权分配：每只约 `total_value / max_positions` 资金（受可用现金上限截断）
- 加权分配：有 `buy_weights` 时每只买入 `total_value × weight[symbol]`
- `sell_shares` 对部分减仓的 symbol 只卖出指定股数，其余持仓保留

**目标市值调仓**（`target_value`）：
- 引擎计算 diff = target - current
- 先卖出超配部分，再买入缺额部分
- trigger = `"TARGET"`

**通用保障**（所有买入路径）：
- 滑点自动应用（买+滑点、卖-滑点）
- 成交量 cap（`order_volume_ratio`）
- 现金不足自动缩股（每次减 100 股直到可负担）
- 缩股后 < 100 股取消该买单

**T+1 锁定**（所有买卖路径生效）：

- 买入当日 `Holding.locked = True`，次日解锁。锁定期间条件单自动跳过该持仓——不会出现当天买入当天止损卖出的情况。

**涨跌停边界**（所有买卖路径生效）：

- **涨停不买**：`fill_price >= up_limit` → 跳过该买单。`up_limit` 为 `NaN` 时无法判定涨跌停，买卖双双跳过。
- **跌停不卖**：`fill_price <= down_limit` → 跳过该卖单。同理 `down_limit` 为 `NaN` 时跳过。
- **price NaN 跳过**：条件单 `price` 为 `NaN` 或 bar 关键价格缺失时，该订单静默跳过。

### 5.6 bar 数据契约

`select()` 收到的 `bars` dict-of-dicts 包含以下列：

**数据契约列**（始终存在）：
`open`, `high`, `low`, `close`, `vol`, `adj_factor`, `pre_close`, `up_limit`, `down_limit`

**引擎派生列**（始终存在，从基础列计算）：
`open_hfq`, `high_hfq`, `low_hfq`, `close_hfq`, `pct_chg`

**引擎伪列**（按需附着）：
`industry`（行业分类字符串）、`log_mktcap`（log 总市值）、`idx_ret`（基准指数日收益率）

**因子列**：`FACTOR_SPECS` 引用的所有因子（含传递闭包）被物化为同名列。

**扩展字段**：`extra_fields` 中登记的任何列（如 `pe_ttm`、`turnover_rate`）。

**前视保护**：引擎提供 `DataProvider` 的 `_as_of_date` 机制。`select()` 中通过 `provider.get_historical_bars()` 查询历史数据时，引擎确保只返回当前日期之前的数据。

---

## 6. 从简单到复杂：逐级教程

每个级别基于 `strategies/examples/` 中的真实代码，聚焦新展示的能力。

### 6.1 Level 0：裸因子轮动

> 完整代码：`strategies/examples/simple_rotation/`

**展示能力**：最简策略的完整骨架。

**strategy.py**：

```python
from btcore.filters import StockFilter
from btcore.strategy import Strategy
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs

class SimpleRotation(Strategy):
    def on_start(self, provider, first_date, end_date=None):
        self._top_k = int(self.config.get("top_k", 5))
        self._filter = StockFilter(provider.backend, first_date, self.FILTER_RULES, end_date=end_date)
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

    def select(self, bars, account_snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self._filter.filter(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        target = set(score.sort_values(ascending=False).head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())
        self._cond.prune(current)

        return {"buy": sorted(target - current), "sell": sorted(current - target)}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return self._cond.calc(symbol, entry_price, bar, holding_days)
```

**config.yaml**：

```yaml
strategy: strategies.examples.simple_rotation:SimpleRotation
config:
  max_positions: 10
  top_k: 5
factor_specs:
  - factor: mom20
    weight: 1.0
    ascending: false
  - factor: low_turnover
    weight: 0.5
    ascending: true
filter_rules:
  exclude_st: true
  exclude_new_stock: true
  min_price: 3.0
conditions:
  stop_loss_pct: 0.08
```

**关键点**：
- 三板斧：`StockFilter` 过滤 → `eval_factor_specs` 打分 → `ConditionBuilder` 条件单
- 无 `on_fills`、无 `buy_weights`、无 `schedule`——每日调仓
- `self._cond.prune(current)` 清理已平仓标的的 trailing 状态

### 6.2 Level 1：进阶轮动

> 完整代码：`strategies/examples/topk_momentum/`

**新增能力**：`on_fills` 成交感知 + `on_tick` 每日维护 + `buy_weights` 加权分配 + `holding_days` 动态调参。

**strategy.py** 关键新增：

```python
class TopKMomentum(Strategy):
    REQUIRED_FIELDS: list[str] = []  # 无额外列需求

    def on_start(self, provider, first_date, end_date=None):
        # ... (同 Level 0)
        self._cooldown_days = int(self.config.get("cooldown_days", 3))
        self._cooldown: dict[str, int] = {}  # 冷却期 map

    def on_fills(self, trades, provider):
        """条件单退出的标的进入冷却期。"""
        for t in trades:
            if t.side == "SELL" and t.trigger in ("STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP"):
                cd = self._cooldown_days * 2 if t.trigger == "STOP_LOSS" else self._cooldown_days
                self._cooldown[t.symbol] = int(t.date) + cd

    def on_tick(self, bars, snapshot, provider):
        """冷却期递减 + 条件单状态修剪。"""
        if not bars:
            return
        date_str = next(iter(bars.values())).get("trade_date", "")
        date_int = int(date_str) if date_str else 0
        expired = [s for s, d in self._cooldown.items() if d <= date_int]
        for s in expired:
            del self._cooldown[s]
        self._cond.prune(set(snapshot.holdings.keys()))

    def select(self, bars, account_snapshot, provider):
        # ... 过滤 + 打分
        # 排除冷却期标的
        score = score[~score.index.isin(self._cooldown)]
        # ... 选 top_k
        # 买入权重：按得分比例分配
        if buy_list:
            raw = score.loc[buy_list].clip(lower=0)
            total = raw.sum()
            buy_weights = {sym: float(raw[sym] / total * 0.9) for sym in buy_list} if total > 0 else None
        return {"buy": buy_list, "sell": sell_list, "buy_weights": buy_weights}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)
        for c in conds:
            if c.get("type") == "STOP_LOSS":
                if holding_days <= 3:
                    c["price"] = entry_price * 0.97       # 新仓保护
                elif holding_days > 30:
                    c["price"] = entry_price * 0.85       # 老仓放宽
            elif c.get("type") == "TAKE_PROFIT" and holding_days <= 3:
                conds.remove(c)                           # 新仓不给止盈
        return conds
```

**关键点**：
- `on_fills` 感知 `Trade.trigger` 区分冷却期强度（STOP_LOSS 冷却翻倍）
- `on_tick` 每日递减冷却期——绕过 schedule 的拦截
- `buy_weights` 按得分比例分配资金，总和 < 1 以保留现金缓冲
- `calc_conditions` 中 `holding_days` 自适应调参——前 3 天紧止损、30 天后放宽

### 6.3 Level 2：目标仓位调仓

> 完整代码：`strategies/examples/target_allocator/`

**新增能力**：`target_value` 精确仓位管理 + `risk_rules` 组合风控 + `schedule` 周频调仓 + `sell_shares` 部分减仓。

**strategy.py** 关键部分：

```python
class TargetAllocator(Strategy):
    def select(self, bars, account_snapshot, provider):
        # ... 过滤 + 打分 + 选 top_k
        top_symbols = score.sort_values(ascending=False).head(self._top_k)
        current = set(account_snapshot.holdings.keys())

        total_value = account_snapshot.total_value
        allocable = total_value * 0.95
        target_value: dict[str, float] = {}

        # 不在 top_k 的持仓：target = 0（清仓）
        for sym in current:
            if sym not in top_symbols.index:
                target_value[sym] = 0.0

        # 在 top_k 的：按得分比例分配
        raw_w = top_symbols.clip(lower=0)
        w_sum = raw_w.sum()
        if w_sum > 0:
            for sym in top_symbols.index:
                target_value[sym] = float(allocable * raw_w[sym] / w_sum)

        # sell_shares：top_k*1.5 内的减半而非清仓
        near_top = set(score.nlargest(int(self._top_k * 1.5)).index)
        for sym in list(target_value):
            if target_value[sym] == 0.0 and sym in near_top:
                h = account_snapshot.holdings.get(sym)
                if h and h.shares >= 200:
                    target_value[sym] = h.last_price * h.shares * 0.5

        return {"buy": [], "sell": [], "target_value": target_value}
```

**config.yaml**：

```yaml
schedule:
  frequency: weekly
  weekday: -1
risk_rules:
  max_drawdown: 0.12
  cooldown_days: 5
  max_position_pct: 0.10
config:
  execution_price: "close"
```

**关键点**：
- `target_value` 返回格式——引擎自动计算买卖差额，trigger = `"TARGET"`
- `sell_shares` 用于近边缘持仓的部分保留（降低换手率）
- `schedule: weekly` 降低调仓频率
- `risk_rules` 提供熔断 + 单票上限
- `execution_price: "close"` 以收盘价成交

### 6.4 Level 3：条件单猎手

> 完整代码：`strategies/examples/condition_hunter/`

**新增能力**：`buy_conditions` 条件买入 + 自定义离场 handler + 自定义入场 handler。

**strategy.py** 关键部分：

```python
from btcore.match.conditions import register_condition_handler, register_buy_condition_handler

# 自定义离场：波动率自适应止损
def _dynamic_stop_handler(holding, cond, bar):
    vol_ratio = cond.get("vol_ratio", 0.05)
    width = max(0.03, min(0.10, vol_ratio * 0.02))
    stop_price = holding.entry_price * (1 - width)
    if bar["open"] <= stop_price:
        return (True, bar["open"], {})
    if bar["low"] <= stop_price:
        return (True, stop_price, {})
    return (False, 0.0, {})

# 自定义入场：VWAP 均价买入
def _vwap_buy_handler(order, bar):
    h, lo, c = bar.get("high"), bar.get("low"), bar.get("close")
    if not all([h, lo, c]):
        return (False, 0.0, {})
    vwap_est = (h + lo + c) / 3
    if bar["open"] <= vwap_est:
        return (True, bar["open"], {})
    if bar["low"] <= vwap_est:
        return (True, vwap_est, {})
    return (False, 0.0, {})

class ConditionHunter(Strategy):
    def on_start(self, provider, first_date, end_date=None):
        register_condition_handler("DYNAMIC_STOP", _dynamic_stop_handler)
        register_buy_condition_handler("VWAP_BUY", _vwap_buy_handler)
        # ... 其余初始化

    def select(self, bars, account_snapshot, provider):
        # ... 主仓 top_k
        # 条件买入：top_k 之后的 hunt_count 只作为条件买单候选
        buy_conditions = []
        total_value = account_snapshot.total_value
        hunt_size = total_value * 0.02  # 每只 2% 试探

        candidates = sorted_score.iloc[self._top_k:self._top_k + self._hunt_count]
        for sym in candidates.index:
            close = filtered.get(sym, {}).get("close", 0) or 0
            if close > 0:
                buy_conditions.append({
                    "symbol": sym, "type": "LIMIT_BUY",
                    "price": round(close * 0.98, 2), "value": hunt_size,
                })
                buy_conditions.append({
                    "symbol": sym, "type": "VWAP_BUY",
                    "price": round(close * 0.99, 2), "value": hunt_size,
                })
        return {"buy": buy_list, "sell": sell_list, "buy_conditions": buy_conditions}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        conds = self._cond.calc(symbol, entry_price, bar, holding_days)
        # 自定义止损替换标准止损
        conds = [c for c in conds if c.get("type") != "STOP_LOSS"]
        conds.append({"type": "DYNAMIC_STOP", "price": None, "vol_ratio": est_vol})
        return conds
```

**config.yaml**：

```yaml
config:
  condition_slippage_ticks: 1   # 条件单独立滑点
  hunt_count: 4
conditions:
  take_profit_pct: 0.20
  trailing_pct: 0.08
  # 注意：没有 stop_loss_pct——被 DYNAMIC_STOP 替代
```

**关键点**：
- `buy_conditions` 与 `buy` 名单可共存——主仓 + 试探仓
- 自定义 handler 在 `on_start` 中注册，进程级全局
- 条件买入自动受涨停不买、成交量 cap、现金不足缩股等约束
- `condition_slippage_ticks` 独立于手动买卖滑点

### 6.5 Level 4：状态机多模型策略

> 完整代码：`strategies/examples/state_machine/`

**新增能力**：自定义因子库 + 市场状态机 + 多模型投票 + 精确持仓状态跟踪 + 全部 hook 协同。

**config.yaml** 关键配置：

```yaml
factor_library: factors.yaml    # 同目录自定义因子库
schedule:
  frequency: weekly
  weekday: -1
factor_specs:                   # 11 个因子（含坍缩算子构建的市场广度因子）
  - factor: mom20_z
    weight: 0.15
  - factor: mkt_breadth20      # 坍缩算子——全市场站上 MA20 占比
    weight: 0.06
# ... 等
risk_rules:
  max_drawdown: 0.15
  cooldown_days: 7
  max_position_pct: 0.10
```

**factors.yaml**（自定义因子库）：

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"
  mom20_z:
    expr: "zscore(mom20)"
  mkt_breadth20:
    expr: "mean(close_hfq > ma(close_hfq, 20))"    # 坍缩：全市场聚合后广播
  mkt_up_ratio:
    expr: "mean(close_hfq > delay(close_hfq, 1))"   # 上涨占比
  idiosyncratic_vol:
    expr: "resid_std(close_hfq, open_hfq, 20)"       # 特质波动
  # ... 更多因子
```

**strategy.py** 核心架构：

```python
class StateMachine(Strategy):
    REQUIRED_FIELDS: list[str] = ["turnover_rate"]

    def on_start(self, provider, first_date, end_date=None):
        # 注册 3 个自定义 handler
        register_condition_handler("DYNAMIC_STOP", _dynamic_stop)
        register_condition_handler("TIME_STOP", _time_stop)
        register_condition_handler("VOLATILITY_EXIT", _volatility_exit)

        # 市场状态机
        self._regime: str = "neutral"           # bull | bear | neutral
        self._regime_counter: int = 0
        self._mode_confirm = int(self.config.get("mode_confirm_days", 5))

        # 仓位乘数
        self._position_mult = {"bull": 1.0, "neutral": 0.85, "bear": 0.60}

        # 3 套子模型因子规格
        self._momentum_specs = [
            {"name": "mom20_z", "weight": 0.4}, ...
        ]
        self._reversal_specs = [
            {"name": "mom5_rev", "weight": 0.5, "ascending": True}, ...
        ]
        self._quality_specs = [
            {"name": "idiosyncratic_vol", "weight": 0.35, "ascending": True}, ...
        ]

        # 持仓状态跟踪（穿透 on_fills / select / calc_conditions）
        self._holding_state: dict[str, dict] = {}
        self._cooldown_map: dict[str, int] = {}

    def on_fills(self, trades, provider):
        """精确持仓状态跟踪——加权均价、最高价、入场日期。"""
        for t in trades:
            if t.side == "SELL" and t.trigger in (...):
                self._cooldown_map[t.symbol] = date_int + cd
                self._holding_state[t.symbol]["exit_trigger"] = t.trigger
            elif t.side == "BUY":
                entry = self._holding_state.get(t.symbol, {})
                # 加权均价更新
                old_cost = entry.get("entry_price", t.price) * entry.get("total_shares", 0)
                new_cost = t.price * t.shares
                entry["entry_price"] = (old_cost + new_cost) / (old_shares + t.shares)
                entry["highest_price"] = max(entry.get("highest_price", 0), t.price)
                self._holding_state[t.symbol] = entry

    def on_tick(self, bars, snapshot, provider):
        """每日：冷却递减 + 最高价跟踪 + 市场状态机推进。"""
        # 冷却递减...
        # 逐仓最高价跟踪...
        # 市场状态检测（用坍缩因子 mkt_breadth20 / mkt_up_ratio）
        _, breadth_score = eval_factor_specs(df, [
            {"name": "mkt_breadth20", "weight": 0.6},
            {"name": "mkt_up_ratio", "weight": 0.4},
        ])
        breadth = breadth_score.mean()
        # 状态切换需连续 N 日确认
        if breadth > 0.65:
            self._regime_counter = min(self._regime_counter + 1, self._mode_confirm)
        elif breadth < 0.35:
            self._regime_counter = max(self._regime_counter - 1, -self._mode_confirm)
        # 切换阈值判断 → bull / bear / neutral

    def select(self, bars, account_snapshot, provider):
        # 3 模型独立打分
        _, mom_score = eval_factor_specs(df, self._momentum_specs)
        _, rev_score = eval_factor_specs(df, self._reversal_specs)
        _, qual_score = eval_factor_specs(df, self._quality_specs)

        # 按市场态动态加权
        weights = {
            "bull":    {"momentum": 0.55, "reversal": 0.15, "quality": 0.30},
            "neutral": {"momentum": 0.35, "reversal": 0.30, "quality": 0.35},
            "bear":    {"momentum": 0.20, "reversal": 0.50, "quality": 0.30},
        }[self._regime]
        total_score = (
            mom_score.fillna(0) * weights["momentum"]
            + rev_score.fillna(0) * weights["reversal"]
            + qual_score.fillna(0) * weights["quality"]
        )

        # 仓位乘数调整 top_k
        effective_top_k = max(2, int(self._top_k * self._position_mult[self._regime]))

        # 选股 + buy_weights + sell_shares + buy_conditions ...
        return {"buy": buy_list, "sell": sell_list, "buy_weights": ..., "sell_shares": ..., "buy_conditions": ...}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        conds = self._cond.calc(...)                 # 内置三件套
        conds.append({"type": "DYNAMIC_STOP", ...})  # 波动率自适应
        conds.append({"type": "TIME_STOP", "max_days": 60})  # 60 日强制退出
        conds.append({"type": "VOLATILITY_EXIT", "threshold": 0.07})  # 异常振幅退出
        # holding_days 动态调参...
        return conds
```

**关键点**：
- 坍缩算子（`mean`）在 `on_tick` 中检测全市场广度，驱动市场状态机——策略利用了引擎的"两路面板供给"机制（广度面板自动聚合后广播回主面板）
- 3 套因子规格独立打分后用加权平均合成，权重随市场态动态切换
- `on_fills` 中加权均价更新——正确处理加仓场景
- `on_tick` 不受 schedule 拦截——市场状态检测每日运行，但 `select` 只在周末调仓
- 4 层条件单：内置三件套 + 3 种自定义，构成完整离场系统

---

## 7. 进阶模式与技巧

### 7.1 多模型投票/集成

Level 4 的模式可归纳为通用范式：

1. 在 `on_start` 中定义 N 套 `factor_specs`（每套是 `[{name, weight, ascending}]` 列表）
2. 在 `select` 中分别调用 `eval_factor_specs` 获取各模型得分
3. 用静态或动态权重加权合成
4. 动态权重由 `on_tick` 中的市场状态检测驱动

```python
# 模板
self._models = {
    "trend":  [{"name": "mom20_z", "weight": 1.0}],
    "value":  [{"name": "ep_ttm", "weight": 1.0}],
    "quality":[{"name": "gpr_z", "weight": 0.5}, ...],
}
# 在 select 中
scores = {}
for name, specs in self._models.items():
    _, scores[name] = eval_factor_specs(df, specs)
final = sum(scores[n].fillna(0) * self._model_weights[n] for n in scores)
```

### 7.2 市场状态检测

坍缩算子（`mean` / `group_mean`）是构建市场广度因子的核心工具。引擎自动规划两路面板——在全市场上计算聚合值后投影回候选池：

```yaml
# 市场广度因子示例
factors:
  mkt_breadth20:
    expr: "mean(close_hfq > ma(close_hfq, 20))"
    description: "全市场站上 MA20 的股票占比"
  mkt_pct_up:
    expr: "mean(pct_chg > 0)"
    description: "全市场上涨占比"
  industry_strength:
    expr: "group_mean(roc(close_hfq, 20), industry)"
    description: "行业平均动量（map 回个股）"
```

策略在 `on_tick` 中用 `eval_factor_specs` 读取这些因子，驱动市场状态判断。`on_tick` 每日运行使状态检测不受 schedule 限制。

### 7.3 冷却期管理

两种冷却期触发模式：

**模式 A：`on_fills` 感知条件单退出**（精确，感知实际成交）

```python
def on_fills(self, trades, provider):
    for t in trades:
        if t.side == "SELL" and t.trigger in STOP_TRIGGERS:
            self._cooldown[t.symbol] = int(t.date) + cooldown_days
```

**模式 B：`on_tick` 感知手动卖出**（不依赖 `on_fills`）

```python
def on_tick(self, bars, snapshot, provider):
    # 对不在当前持仓且仍在冷却期的标的递减
    ...
```

在 `select` 中排除冷却期标的：

```python
score = score[~score.index.isin(self._cooldown)]
```

### 7.4 动态参数调整

根据运行时状态调整策略参数的一般模式：

- **持仓天数自适应**：在 `calc_conditions` 中 `if holding_days <= N` 收紧止损/不给止盈
- **市场态仓位乘数**：牛市满仓、震荡 85%、熊市 60%，调整 `top_k` 和 `buy_weights`
- **波动率自适应**：从 `bar["pct_chg"]` 和 `bar["turnover_rate"]` 估算日内波动，动态调整止损宽度
- **市场广度自适应**：广度好时放宽选股条件，广度差时收紧

### 7.5 部分减仓

`sell_shares` 用于"持有但减仓"的场景——不在 top_k 中但排名尚可的持仓：

```python
near_top = set(sorted_score.head(int(self._top_k * 1.3)).index)
for sym in current - target:
    if sym in near_top:
        h = account_snapshot.holdings[sym]
        if h.shares >= 200:
            sell_shares[sym] = h.shares // 2   # 保留一半
```

与 `target_value` 的对比：`target_value` 做精确市值管理（增减到目标值），`sell_shares` 做简单减仓（固定股数）。

### 7.6 条件买入策略

`buy_conditions` 与 `buy` 名单的典型配合模式：

- **主仓**：`buy` 名单持有 top_k 标的，等权或加权
- **试探仓**：对紧随其后的备选标的挂条件买单（小额 1-2% 试探）
- **多类型覆盖**：同一标的挂 LIMIT_BUY（回踩）+ BREAKOUT_BUY（突破），增加成交概率

条件买单当日有效（T 日声明，T+1 日盘中触发），未触发自动失效。不会与 buy 名单冲突。

### 7.7 列裁剪与性能

`REQUIRED_FIELDS` 控制引擎 preload 的数据加载范围：

- **必须声明的列**：`select()` 中通过 `bar["col_name"]` 访问的扩展字段（如 `turnover_rate`、`pe_ttm`）
- **不需要声明**：因子列（`FACTOR_NODES` 自动覆盖）、引擎默认列（`open`/`high`/`low`/`close`/`vol`/`adj_factor` 永不裁剪）
- 不声明而访问 → `KeyError`

```python
REQUIRED_FIELDS: list[str] = ["turnover_rate"]  # 只声明 select 中使用的扩展列
```

---

## 8. 反模式与常见错误

| 错误 | 后果 | 修复 |
|------|------|------|
| `select()` 中访问未在 `REQUIRED_FIELDS` 声明的列 | `KeyError` | 将列名加入 `REQUIRED_FIELDS` |
| 忘记在 `on_start` 中 `register_condition_handler` | 运行时 `ValueError: 未知条件类型` | 在 `on_start` 中注册 |
| `target_value` 与 `buy`/`sell` 同一天混用 | `ValueError` | 二选一 |
| `sell_shares` 包含不在 `sell` 中的 symbol | 引擎静默忽略 | 确保键是 `sell` 子集 |
| `buy_weights` 的键与 `buy` 列表不一致 | `ValueError` | 确保键精确匹配 |
| `ConditionBuilder.prune()` 忘记调用 | trailing 状态泄漏到已平仓标的 | 在 `select()` 或 `on_tick()` 中调用 |
| 条件单 `price` 填 `None` 但自定义 handler 未处理 | 异常 | handler 中自行计算 price |
| 在非调仓日依赖 `select()` 做状态更新 | 非调仓日 `select` 不执行 | 将状态更新移到 `on_tick` |
| `filter_rules.exclude_loss` 声明了但后端无 `pe_ttm` 列 | 静默跳过（不报错也不过滤） | 确保后端 `extra_fields` 有 `pe_ttm` |
| 空 `bars` 时未做提前返回 | `StopIteration` / `KeyError` | `if not bars: return {"buy": [], "sell": []}` |

---

## 9. 运行与调试

### 9.1 CLI 运行

```bash
# 单次回测
python scripts/run.py strategies/my_strategy/config.yaml --start 20240101 --end 20240630

# 指定输出数据库路径
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --db my_result.db
```

### 9.2 查看结果

```bash
# 生成 HTML 报告
python scripts/report.py result.db --out report.html

# 多次运行对比
python scripts/compare.py result.db --html compare.html

# 交叉验证（检查交易合理性）
python scripts/cross_validate.py result.db --strategy name --run-id 1
```

### 9.3 程序化调用

```python
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy

strategy = load_strategy("strategies/my_strategy/config.yaml")
provider = DataProvider(backend)
engine = Engine(strategy, provider, db_path="result.db")

result = engine.run("20240101", "20240630")
# result["account_daily"]   → DataFrame，每日账户快照
# result["trade_log"]      → DataFrame，所有成交
# result["statistics"]     → dict，统计指标（详见下方）
# result["benchmark_nav"]  → list[float] | None，基准净值序列
# result["benchmark_code"] → str | None，基准代码
```

`result["statistics"]` 包含以下关键指标组：

| 指标组 | 包含 | 说明 |
|--------|------|------|
| 收益指标 | `total_return`、`annualized_return`、`sharpe`、`max_drawdown`、`calmar` | 标准绩效指标 |
| 交易磨损 | `trading_friction` — 双边磨损率、年化拖累 (bps)、成本占盈利比、无摩擦对照收益 | 衡量交易成本对收益的侵蚀程度 |
| 持仓复杂度 | `management_complexity` — 单日最大成交笔数、有成交天数占比、单票平均市值 | 评估策略的可执行性（对散户跟单来说，日成交 20 笔的策略不可行） |
| 卖出来源 | `sell_source` — MANUAL / STOP_LOSS / TAKE_PROFIT / TRAILING_TP / RISK / TARGET 各占卖出金额的比例 | 理解退出行为的构成——是自然轮动还是被风控打出 |

```

### 9.4 调试技巧

- `logging` 模块：设置 `logger = logging.getLogger(__name__)` 输出调试信息
- 检查 `snapshot.trades` 在 `select()` 中获取当日成交（与 `on_fills` 收到的同一份数据）
- `provider.get_historical_bars()` 可在 `select()` 中查询历史数据（前视保护自动生效）

---

## 10. 参考速查表

### 10.1 select 返回键一览

| 键 | 类型 | 与 target_value 互斥 | 说明 |
|---|---|---|---|
| `buy` | `list[str]` | 是 | 买入名单 |
| `sell` | `list[str]` | 是 | 卖出名单（全清） |
| `buy_weights` | `dict[str, float] \| None` | 是 | 买入权重 |
| `sell_shares` | `dict[str, int] \| None` | 是 | 部分减仓股数 |
| `buy_conditions` | `list[dict] \| None` | 是 | 条件买单 |
| `target_value` | `dict[str, float] \| None` | — | 目标市值 |

### 10.2 YAML 全部键一览

```
顶层: name, strategy*, config, factor_specs, filter_rules, conditions, risk_rules, schedule, factor_library
config: initial_capital, max_positions, slippage_ticks, condition_slippage_ticks,
        execution_price, commission_rate, min_commission, stamp_tax_rate,
        transfer_fee_rate, benchmark, quiet_skips, order_volume_ratio
        + 用户自定义键
filter_rules: exclude_st, exclude_new_stock, exclude_loss, exclude_boards,
              exclude_industries, min_price, index_universe, factor_universe
conditions: stop_loss_pct, take_profit_pct, trailing_pct
risk_rules: max_drawdown, cooldown_days, max_position_pct, max_industry_pct
schedule: frequency, weekday, monthday
```

### 10.3 条件单类型一览

| 类别 | type 值 | 注册方式 | 触发方向 |
|------|---------|---------|---------|
| 买入 | `LIMIT_BUY` | 内置 | 低吸 |
| 买入 | `BREAKOUT_BUY` | 内置 | 追涨 |
| 买入 | 自定义 | `register_buy_condition_handler(type, handler)` | 自定义 |
| 卖出 | `STOP_LOSS` | 内置 | 止损 |
| 卖出 | `TAKE_PROFIT` | 内置 | 止盈 |
| 卖出 | `TRAILING_TP` | 内置 | 移动止盈 |
| 卖出 | 自定义 | `register_condition_handler(type, handler)` | 自定义 |

handler 签名：

```python
# 离场
def handler(holding: Holding, cond: dict, bar: dict) -> tuple[bool, float, dict]
# 入场
def handler(order: dict, bar: dict) -> tuple[bool, float, dict]
```

### 10.4 Trade.trigger 值一览

| trigger | 含义 | 来源 |
|---------|------|------|
| `"MANUAL"` | 手动买卖（`buy`/`sell` 名单） | `select()` 返回的 buy/sell |
| `"TARGET"` | 目标市值调仓 | `select()` 返回的 target_value |
| `"STOP_LOSS"` | 固定止损 | `conditions.stop_loss_pct` |
| `"TAKE_PROFIT"` | 固定止盈 | `conditions.take_profit_pct` |
| `"TRAILING_TP"` | 移动止盈 | `conditions.trailing_pct` |
| `"RISK"` | 风控强制清仓 | `risk_rules.max_drawdown` 熔断 |
| 自定义 | 自定义条件 | `register_condition_handler` 注册的 type |

### 10.5 Holding 属性一览

| 属性 | 类型 | 说明 |
|------|------|------|
| `symbol` | `str` | 股票代码 |
| `shares` | `int` | 持仓股数 |
| `entry_date` | `str` | 入场日期 (YYYYMMDD) |
| `entry_price` | `float` | 入场均价（公司行为调整后） |
| `cost` | `float` | 持仓总成本 |
| `last_price` | `float` | 最新市价 |
| `holding_days` | `int` | 持仓天数 |
| `conditions` | `list[dict]` | 当日计算的条件单列表（引擎附着） |
| `locked` | `bool` | T+1 锁定（买入当日不可卖出） |

### 10.6 引擎工具函数一览

```python
from btcore.strategy_tools import bars_to_df, eval_factor_specs, ConditionBuilder
from btcore.filters import StockFilter
from btcore.match.conditions import register_condition_handler, register_buy_condition_handler

# bars_to_df(bars: dict[str, dict]) -> pd.DataFrame
#   dict-of-dicts → symbol 索引的 DataFrame

# eval_factor_specs(df: DataFrame, specs: list[dict]) -> tuple[DataFrame, Series]
#   读物化因子列 → (factor_df, composite_score)
#   每个 spec: {name: str, weight: float, ascending: bool}

# ConditionBuilder(rules: dict)
#   .calc(symbol, entry_price, bar, holding_days) -> list[dict]
#   .prune(live_symbols: set) -> None

# StockFilter(backend, start_date, rules, end_date=None)
#   .filter(bars: dict, date_str: str) -> dict  # 返回过滤后的 bars

# register_condition_handler(type: str, handler: callable) -> None
# register_buy_condition_handler(type: str, handler: callable) -> None
```

### 10.7 结果库 SQLite Schema

回测结果以 SQLite 数据库落盘，以下为三张核心表的结构。同一 `db_path` 多次 `engine.run()` 按 `run_id` 增量追加，互不覆盖；run 中抛异常时 `status` 改写为 `failed`。

**runs**（每次回测的元信息）：

| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER PK AUTOINCREMENT | 运行编号 |
| `created_at` | TEXT | 创建时间戳 |
| `strategy` | TEXT | 策略类名 |
| `start_date` / `end_date` | TEXT | 回测区间 (YYYYMMDD) |
| `initial_capital` | REAL | 初始资金（元） |
| `config_json` | TEXT | 策略完整配置 JSON |
| `status` | TEXT | `running` / `completed` / `failed` |
| `stats_json` | TEXT | 统计指标 JSON |

**account_daily**（逐日账户快照）：

| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER | 关联 runs |
| `date` | TEXT | 交易日 (YYYYMMDD) |
| `cash` | REAL | 可用现金 |
| `total_value` | REAL | 总资产（现金 + 持仓市值） |
| `daily_pnl` | REAL | 当日盈亏 |
| `cumulative_pnl` | REAL | 累计盈亏 |
| `initial_capital` | REAL | 初始资金 |
| `n_holdings` | INTEGER | 持仓数 |

**trade_log**（逐笔成交记录）：

| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER | 关联 runs |
| `date` | TEXT | 成交日期 (YYYYMMDD) |
| `symbol` | TEXT | 股票代码 |
| `side` | TEXT | `BUY` / `SELL` / `DIV`（公司行为） |
| `trigger` | TEXT | 触发类型（见 §10.4） |
| `price` | REAL | 成交价（已含滑点） |
| `shares` | INTEGER | 成交股数 |
| `turnover` | REAL | 成交金额 |
| `commission` | REAL | 佣金 |
| `stamp_tax` | REAL | 印花税（仅卖出） |
| `transfer_fee` | REAL | 过户费 |
| `slippage_amount` | REAL | 滑点金额 |
| `net_amount` | REAL | 净现金流 |
| `reason` | TEXT | 备注 |

---

## 11. 与现有示例的对照

| 示例 | 文件 | 展示的核心能力 |
|------|------|---------------|
| simple_rotation | `strategies/examples/simple_rotation/` | 最简骨架：StockFilter + eval + ConditionBuilder |
| topk_momentum | `strategies/examples/topk_momentum/` | on_fills + on_tick + buy_weights + 动态参数 |
| target_allocator | `strategies/examples/target_allocator/` | target_value + risk_rules + schedule + sell_shares |
| condition_hunter | `strategies/examples/condition_hunter/` | buy_conditions + 自定义 handler |
| state_machine | `strategies/examples/state_machine/` | 全部能力：状态机 + 多模型 + 坍缩因子 + 精确跟踪 |

建议按此顺序阅读代码：先理解 simple_rotation 的基本骨架，再逐级叠加新能力，最后看 state_machine 的全景。
