# 策略设计指南

ddup 策略系统由两部分构成：**YAML 配置**声明因子引用、过滤规则和条件单；**Python 策略类**实现 `select` 核心决策方法（`on_start` / `calc_conditions` 有基类默认实现，覆盖时才需自己写）。引擎负责 preload、因子物化、撮合和结果落库——策略代码只描述买卖决策。

---

## 1. 快速开始

最小可运行策略：YAML + Python 两个文件。

**config.yaml**：

```yaml
strategy: strategies.my_strategy:MyStrategy

factor_specs:
  - factor: mom20              # 引用 factors/library.yaml 中定义的因子名
    weight: 1.0
    ascending: false           # false=值越大越好（默认）

config:
  max_positions: 10
  top_k: 5                     # 自定义键，策略代码 self.config.get("top_k") 读取

filter_rules:
  exclude_st: true
  exclude_new_stock: true
  min_price: 3.0

conditions:
  stop_loss_pct: 0.08          # 跌 8% 止损
```

**strategy.py**：

```python
from btcore.strategy import Strategy
from btcore.strategy_tools import bars_to_df, eval_factor_specs

class MyStrategy(Strategy):
    def on_start(self, provider, first_date, end_date=None):
        # 基类接线：FILTER_RULES → self._filter、conditions → self._cond
        super().on_start(provider, first_date, end_date)
        self._top_k = int(self.config.get("top_k", 5))

    def select(self, bars, account_snapshot, provider):
        if not bars:
            return {"buy": [], "sell": []}

        date_str = next(iter(bars.values())).get("trade_date", "")
        filtered = self.filter_bars(bars, date_str)

        df = bars_to_df(filtered)
        _, score = eval_factor_specs(df, self.FACTOR_SPECS)

        target = set(score.sort_values(ascending=False).head(self._top_k).index)
        current = set(account_snapshot.holdings.keys())

        return {"buy": sorted(target - current), "sell": sorted(current - target)}

    # calc_conditions 未覆盖：基类默认实现把 YAML conditions 节翻译为条件单
```

**运行**：

```bash
python scripts/run.py strategies/my_strategy/config.yaml --start 20240101 --end 20240630
```

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
       ├─ on_fills(trades)        ← 感知当日已撮合成交       │
       ├─ on_tick(bars, snapshot) ← 每日状态维护 + buy_conditions │
       ├─ select(bars, snapshot) → {buy, sell, ...}          │
       ├─ calc_conditions() × N   ← 每个持仓生成条件单       │
       └─ 生成明日 pending_actions ──────────────────────────┘
```

`select` 生成的是**下一交易日**执行的买卖信号，当日不成交——T+1 机制的源头。当日撮合的是前一日 `select` 输出的 pending_actions。`on_fills` / `on_tick` 可选；`on_tick` 与 `calc_conditions` 每日都运行，不受策略内部调仓节奏影响。

**设计契约（策略作者须知）**：

- **价格体系**：撮合、成本、估值用裸价（`open` / `close`）；因子计算、排名用后复权（`*_hfq`）。混用会让除权日的价格跳空变成虚假信号。
- **软回退与 Fail-Fast**：可选能力缺失（ST 表、行业表、指数成分表）→ 告警一次后继续运行，对应规则不生效；明确声明的依赖缺失（因子引用的伪列无后端支持、必需列缺失、因子名不存在、未注册的条件单类型）→ 加载或决策阶段直接报错。

### 2.2 策略作者职责一览

| 职责 | 方式 | 说明 |
|------|------|------|
| 必须实现 | `select(bars, snapshot, provider)` → dict | 每日买卖决策 |
| 可选覆盖 | `on_start(provider, first_date, end_date)` | 默认已构建 `self._filter`（FILTER_RULES 非空时）；覆盖需 `super().on_start(...)` |
| 可选覆盖 | `calc_conditions(symbol, entry_price, bar, holding_days)` → list[dict] | 默认委托 `self._cond.calc()` 翻译 YAML conditions；覆盖需自行处理 `self._cond` |
| 可选实现 | `on_fills(trades, provider)` | 感知成交 → 冷却期、状态跟踪 |
| 可选实现 | `on_tick(bars, snapshot, provider)` → dict \| None | 每日状态维护 + 非调仓日条件买单；默认实现负责 `ConditionBuilder.prune()`，覆盖时请 `super().on_tick(...)` |
| 可选覆盖 | `get_universe(provider, start, end)` → list[str] \| None | 自定义交易域 |
| 可选覆盖 | `get_factor_universe(provider, start, end)` → list[str] \| None | 自定义因子计算域 |
| 声明式 | `REQUIRED_FIELDS: list[str]` | 声明 `select()` 中命令式访问的列 |
| 声明式 | `FACTOR_SPECS: list[dict]` | 因子引用列表（YAML 或类变量） |
| 声明式 | `FILTER_RULES: dict` | 过滤规则（YAML 或类变量） |
| 声明式 | `CONDITION_FACTORS: set[str]` | 条件单读取的因子名，加载时参与交叉校验 |

### 2.3 引擎提供的工具

| 工具 | 位置 | 用途 |
|------|------|------|
| `bars_to_df(bars)` | `btcore.strategy_tools` | dict-of-dicts → symbol 索引 DataFrame |
| `eval_factor_specs(df, specs)` → (DataFrame, Series) | `btcore.strategy_tools` | 读物化因子列 → 合成得分 (0~1) |
| `ConditionBuilder(rules)` | `btcore.strategy_tools` | 声明式条件单构建 + trailing 状态跟踪 |
| `StockFilter(backend, start, rules)` | `btcore.filters` | 截面多规则过滤 |
| `register_condition_handler(type, handler)` | `btcore.match.conditions` | 注册自定义离场条件 |
| `register_buy_condition_handler(type, handler)` | `btcore.match.conditions` | 注册自定义买入条件 |

含完整签名的速查表见 §10.6。

---

## 3. 策略 Python 接口参考

### 3.1 Strategy 基类

```python
from btcore.strategy import Strategy

class MyStrategy(Strategy):
    # 类变量 —— 声明式默认值，YAML 可覆盖
    REQUIRED_FIELDS: list[str] = ["open", "high", "low", "close", "vol", "adj_factor"]
    FACTOR_SPECS: list[dict] = []
    FILTER_RULES: dict = {}
    CONDITION_FACTORS: set[str] = set()
```

`FACTOR_NODES`（因子闭包）与 `MODEL_SPECS`（ML 模型声明）由 strategy_loader 挂接，用户不设置；`MODEL_SPECS` 见 [ML 子系统指南](./ml_guide.md)。

构造函数签名 `__init__(self, config, factor_specs=None, filter_rules=None)`。实例化后：
- `self.config` — YAML `config` 节的完整 dict（顶层 `conditions` 节也由 loader 合并进 `config["conditions"]`）
- `self.FACTOR_SPECS` — `[{name, weight, ascending, materialize_only}]` 格式的因子引用列表
- `self.FILTER_RULES` — 过滤规则 dict

### 3.2 `on_start(self, provider, first_date, end_date=None)`

**可选覆盖。** 基类默认实现：`FILTER_RULES` 非空时构建 `StockFilter` 挂到 `self._filter`。回测开始前调用一次。覆盖时在此初始化所有策略状态：

- 先调用 `super().on_start(provider, first_date, end_date)` 以构建 `self._filter`
- 注册自定义条件单 handler（见 §5.2.3）
- 解析自定义 config 参数：`self._top_k = int(self.config.get("top_k", 5))`
- 初始化状态字典：冷却期 map、持仓跟踪 dict、市场状态变量

> 覆盖但忘记 `super().on_start(...)` 时，`filter_bars()` 会在 `FILTER_RULES` 非空时直接报错（fail-fast），而不是让过滤规则静默失效。

### 3.3 `select(self, bars, account_snapshot, provider)` → dict

**必须实现。** 每个交易日调用。参数：

- `bars`: `dict[str, dict]` — 当日截面数据，键为 symbol，值为 OHLCV + 因子列 + 扩展字段的 dict。引擎保证 `trade_date` 字段存在。
- `account_snapshot`: `Snapshot` 对象：
  - `cash` — 可用现金
  - `holdings` — `dict[str, Holding]`，当前持仓的**深拷贝**（修改不影响引擎状态）
  - `trades` — 当日已执行的成交列表 `list[Trade]`（与 `on_fills` 收到的同一份数据）
  - `total_value` — 总资产（现金 + 持仓市值）
- `provider`: `DataProvider` 对象，提供 `get_historical_bars(symbols, end_date, lookback_days=365)`、`get_benchmark_returns(end_date, lookback_days=252)`、`get_benchmark_trend(end_date, window=30)`（基准近 window 日累计收益）等查询（均带前视保护，见 §5.5）。

**返回值**支持的键：

| 键 | 类型 | 语义 |
|---|---|---|
| `buy` | `list[str]` | 买入名单。空列表 = 无买入。 |
| `sell` | `list[str]` | 卖出名单（默认全部清仓）。空列表 = 无卖出。 |
| `buy_weights` | `dict[str, float] \| None` | 每只买入的资金权重，每项 ∈ (0, 1]，权重和 ≤ 1。引擎按 `total_value × weight` 分配资金。键必须与 `buy` 列表精确一致。`None` = 等权买入。 |
| `sell_shares` | `dict[str, int] \| None` | 部分减仓股数（正整数）。键必须是 `sell` 列表的子集。 |
| `buy_conditions` | `list[dict] \| None` | T+1 日盘中条件买单列表，格式见 §5.2.2。 |
| `target_value` | `dict[str, float] \| None` | 每只股票的目标市值，引擎自动计算买卖差额。`0` = 清仓；未出现的 symbol 不动。键必须是非空字符串、值必须 ≥ 0 的有限数值，否则 `ValueError`。与 `buy`/`sell`/`buy_conditions` 同日互斥。 |

**冲突校验**（引擎在决策时点执行，违规即 `ValueError`，fast-fail）：

| 违规 | 错误 |
|---|---|
| `buy` 与 `sell` 有交集 | `ValueError: 同日买卖冲突` |
| `buy`/`sell` 名单含非字符串或空字符串元素 | `ValueError` |
| `target_value` 与 `buy`/`sell` 同时非空 | `ValueError: 互斥` |
| `target_value` 与 `buy_conditions` 同时非空 | `ValueError: 互斥` |
| `buy_weights` 的键与 `buy` 列表不一致，或权重 ∉ (0,1]，或权重和 > 1 | `ValueError` |
| `sell_shares` 的键不在 `sell` 列表中，或值不是正整数 | `ValueError` |
| `buy_conditions` 的 symbol 出现在 `buy` 或 `sell` 名单中 | `ValueError` |
| 条件买单缺 `symbol`/`type`/`price`、`price` 非正数、`value` 与 `shares` 同时填或同时缺 | `ValueError` |
| 条件单/条件买单的 `type` 未注册 | `ValueError: 未注册的条件单类型` |

注意：`buy` 名单与 `buy_conditions` **可以同日返回**，但 symbol 不得重叠（典型用法：主仓 buy 名单 + 备选标的挂条件买单，见 `condition_hunter` 示例）。

**空返回** `{"buy": [], "sell": []}` 完全合法——今日不做任何操作。

### 3.4 `calc_conditions(self, symbol, entry_price, bar, holding_days)` → list[dict]

**可选覆盖。** 基类默认实现委托 `ConditionBuilder.calc()`（把 YAML `conditions` 节翻译为条件单）。引擎对**每个持仓每日**调用。返回条件单 dict 列表；空列表 = 该持仓无条件单。

- `symbol` — 股票代码
- `entry_price` — 入场均价（公司行为调整后）
- `bar` — 该 symbol 当日 bar dict
- `holding_days` — 持仓天数（含本日）

每条条件单至少包含 `type`（str）；其余键由对应 handler 自行定义（内置止损/止盈 handler 消费 `price`，`ML_EXIT` 消费 `model`/`score`，自定义 handler 可定义任意键）。引擎按**列表顺序**评估，首条触发生效后不再检查后续条件。返回的 `type` 在决策时点即校验——未注册的类型或缺必填键（如内置类型的 `price`）当天就报错，不会拖到次日撮合。

覆盖扩展：先 `conds = self._cond.calc(symbol, entry_price, bar, holding_days)` 拿默认条件单，再增删改（如 `holding_days` 自适应、替换 `STOP_LOSS`）。

### 3.5 `on_fills(self, trades, provider)`

**可选 hook。** 引擎在 `select` 之前调用，传入当日实际成交的 `Trade` 列表，无成交时为空列表。回测首日前的预跑也会以空列表调用一次。

`Trade` 属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `date` | `str` | 成交日期 (YYYYMMDD) |
| `symbol` | `str` | 股票代码 |
| `side` | `str` | `"BUY"` 或 `"SELL"` |
| `trigger` | `str` | 触发类型（见 §10.4） |
| `price` | `float` | 成交价（已含滑点） |
| `shares` | `int` | 成交股数 |
| `turnover` | `float` | 成交金额（未含滑点的原始成交额） |
| `commission` | `float` | 佣金 |
| `stamp_tax` | `float` | 印花税 |
| `transfer_fee` | `float` | 过户费 |
| `slippage_amount` | `float` | 滑点金额 |
| `net_amount` | `float` | 净现金流 |
| `reason` | `str` | 备注 |

典型用途：感知条件单止损/止盈退出 → 对标的施加冷却期；记录入场价、最高价；重置 trailing 锚点。

### 3.6 `on_tick(self, bars, snapshot, provider)` → dict | None

**可选 hook。** 每日调用，在 `on_fills` 之后、`select` 之前。基类默认返回 `None`。

典型用途：
- 冷却期递减计数
- 市场状态机推进（广度检测 → 状态切换）
- 逐仓最高价跟踪
- `ConditionBuilder.prune()` 清理已平仓标的的 trailing 状态

**返回值**：可返回 `{"buy_conditions": [{symbol, type, price, value|shares}]}`，引擎将其合并进 `select()` 返回的 `buy_conditions`（合并后统一走 §3.3 的校验）。策略因此可以在非调仓日提交条件买单（如突破买入），不受调仓窗口限制。**返回值只支持 `buy_conditions` 一个键**——返回 `buy`/`sell`/`target_value` 等其他键会直接 `ValueError`（买卖名单只能经 `select()` 返回；`on_tick` 里返回这些键此前会被静默丢弃，故引擎改为报错而非忽略）：

```python
def on_tick(self, bars, snapshot, provider):
    # ... 冷却递减、状态维护 ...
    orders = []
    for symbol in self._watchlist:
        bar = bars.get(symbol)
        if bar and bar["high"] >= self._breakout_prices.get(symbol, float("inf")):
            orders.append({
                "symbol": symbol,
                "type": "BREAKOUT_BUY",
                "price": self._breakout_prices[symbol],
                "value": self._position_size,
            })
    return {"buy_conditions": orders} if orders else None
```

### 3.7 `get_universe(self, provider, start, end)` → list[str] | None

**可选覆盖。** 返回策略交易域的股票符号列表；基类返回 `None`（全市场）。引擎 preload 阶段调用，裁剪数据加载范围。

若 YAML `filter_rules` 配置了 `index_universe` 且策略未覆盖此方法，loader 自动生成实现（多指数成分区间并集）。手动覆盖后 `index_universe` 的自动裁剪不再生效（但 StockFilter 的逐日成分过滤仍生效）。

### 3.8 `get_factor_universe(self, provider, start, end)` → list[str] | None

**可选覆盖。** 返回因子计算域的股票符号列表，可以宽于交易域；基类返回 `None`（因子计算域 = 交易域）。`filter_rules.factor_universe` 配置且未覆盖时 loader 自动生成。

### 3.9 类变量 `REQUIRED_FIELDS`

声明 `select()` / `on_tick()` 中**命令式访问**的扩展列（`bar["col"]` 形式）。引擎 preload 列裁剪时保留这些列。不需要声明：

- 因子列（`FACTOR_SPECS` / `FACTOR_NODES` 自动覆盖）
- 基础列（`open`/`high`/`low`/`close`/`vol`/`adj_factor` 永不裁剪）

策略访问了 `bar["turnover_rate"]`、`bar["pe_ttm"]`、`bar["amount"]` 这类扩展字段时必须声明，否则 `KeyError`。

```python
REQUIRED_FIELDS: list[str] = ["turnover_rate"]
```

### 3.10 类变量 `CONDITION_FACTORS`

`calc_conditions()` 或自定义条件单 handler 读取、但**不参与 `eval_factor_specs` 评分排名**的因子名集合。加载时引擎自动执行三项交叉校验（全部 WARNING，不阻断）：

| 检查 | 触发条件 | 含义 |
|------|---------|------|
| scoring ∩ materialize_only | 同一因子同时标记评分和仅物化 | 可能是配置笔误 |
| scoring ∩ CONDITION_FACTORS | 评分因子同时声明为条件因子 | 买入可能因同一因子回落而被卖出 |
| CONDITION_FACTORS 未登记 | 声明的条件因子不在 factor_specs 中 | 引擎不会物化该列，bar 读取为 None |

子类覆盖为 `None` 或保持默认空集都跳过检查。声明的因子仍需出现在 `factor_specs` 中才会物化（可用 `materialize_only: true` 避免参与评分）。

```python
class MyStrategy(Strategy):
    # macd_golden / close_vs_bbi 仅在 calc_conditions 的趋势走坏判断中读取
    CONDITION_FACTORS = {"macd_golden", "close_vs_bbi"}
```

---

## 4. YAML 配置完整参考

### 4.1 顶层键

| 键 | 必需 | 类型 | 说明 |
|---|---|---|---|
| `strategy` | **是** | `str` | `"module.path:ClassName"`，指向 `Strategy` 子类 |
| `name` | 否 | `str` | 策略名称（仅标注用，loader 不消费） |
| `config` | 否 | `dict` | 策略配置参数 |
| `factor_specs` | 否 | `list[dict]` | 因子引用列表 |
| `filter_rules` | 否 | `dict` | 股票过滤规则 |
| `conditions` | 否 | `dict` | 声明式离场条件（loader 合并入 `config["conditions"]`） |
| `factor_library` | 否 | `str` | 自定义因子库路径（相对策略 YAML 目录解析），缺省 `factors/library.yaml` |
| `models` | 否 | `dict` | ML 模型声明（panel/holding 双 scope），见 [ML 子系统指南](./ml_guide.md) |

### 4.2 `config` 引擎识别键

以下键被引擎直接消费，均有默认值，全部可选：

| 键 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `initial_capital` | `float` | `1000000` | 初始资金（元） |
| `max_positions` | `int` | `20` | 最大持仓数。所有买入路径达到上限后只记 INFO 日志、不拦截——策略应自行管理持仓数量 |
| `slippage_ticks` | `int` | `2` | 手动买卖滑点 tick 数（1 tick = 0.01 元）。必须是非负整数，否则 `ValueError` |
| `condition_slippage_ticks` | `int \| None` | `None` | 条件单（含条件买入）独立滑点 tick 数；`None` 时沿用 `slippage_ticks`。必须是非负整数或 None，否则 `ValueError` |
| `execution_price` | `str` | `"open"` | 手动订单成交价字段：`"open"`（次日开盘）或 `"close"`（次日收盘），其他值 `ValueError` |
| `commission_rate` | `float` | `0.00015` | 佣金费率（万 1.5） |
| `min_commission` | `float` | `5.0` | 最低佣金（元） |
| `stamp_tax_rate` | `float` | `0.0005` | 印花税费率（仅卖出收取） |
| `transfer_fee_rate` | `float` | `0.00001` | 过户费费率 |
| `benchmark` | `str \| None` | 自动推导 | 基准指数代码。未设置或 `None` 时：`index_universe` 恰为单指数 → 取该指数，否则沪深 300（`000300.SH`）。显式设为空字符串 `""` = 无基准 |
| `quiet_skips` | `bool` | `False` | `True` 时跳过类告警（涨跌停、成交量、现金不足等）降级为 DEBUG |
| `order_volume_ratio` | `float \| None` | `None` | 单笔订单股数 ≤ `int(成交量(手) × ratio) × 100`；`None` 不限制。必须是正数或 None，否则 `ValueError` |
| `ml_log` | `str` | — | `"full"` 时落盘全截面 ML 分数（缺省只落盘决策相关标的），见 ml_guide |

**自定义键**：任何不在此列表的键（`top_k`、`cooldown_days`、`rebalance_interval` 等）策略代码通过 `self.config.get("key")` 自由读取，引擎不干预。

### 4.3 `factor_specs`

```yaml
factor_specs:
  - factor: mom20              # 因子库中的名字（必填）
    weight: 1.0                # 合成权重，默认 1.0
    ascending: false           # false=值越大排名越靠前（默认）
  - factor: vol_z
    weight: 0.4
    ascending: true            # true=值越小排名越靠前（低波异象）
  - factor: pct_above_ma20
    materialize_only: true     # 仅物化为 bars 列供 calc_conditions 读取，不参与评分
```

每条目被 loader 规范化为 `{name, weight, ascending, materialize_only}`，挂接到 `strategy.FACTOR_SPECS`。`materialize_only: true` 的因子只物化不参与 `eval_factor_specs` 的加权合成——无需编造假权重来触发物化。引用的因子名必须在因子库中存在，否则加载时报错。

### 4.4 `filter_rules`

| 键 | 类型 | 默认 | 需要后端能力 | 说明 |
|---|---|---|---|---|
| `exclude_st` | `bool` | `False` | `st_symbol` | 排除 ST（日频快照：当日有记录才是 ST，摘帽次日恢复可买）。未声明 = 不过滤 |
| `exclude_new_stock` | `bool` | `False` | `listing_date` | 排除上市 60 日内的新股。未声明 = 不过滤 |
| `exclude_loss` | `bool` | `False` | `pe_ttm` 列 | 排除 `pe_ttm <= 0` 的亏损股。未声明 = 不过滤 |
| `exclude_boards` | `list[str]` | `[]` | — | 排除板块：`"BJ"`（北交所）、`"688"`（科创板）、`"300"`/`"301"`（创业板） |
| `exclude_industries` | `list[str]` | `[]` | `industry_name` | 排除指定行业名 |
| `min_price` | `float` | `0.0` | — | 最低收盘价过滤 |
| `index_universe` | `list[str]` | — | `index_code` + `index_member` | 仅交易指数成分股（多指数并集）。白名单只管入场，持仓被调出指数不强制卖出 |
| `factor_universe` | `list[str]` | — | `index_code` + `index_member` | 因子计算域（可宽于交易域）。不填 = 沿用交易域 |

要点：

- 未知键 → WARNING 并被忽略（不报错）。
- `exclude_st` / `exclude_new_stock` / `exclude_loss` 三个布尔规则**默认关闭**：未声明 = 不过滤、不 preload 对应列、也不告警（不再“假开启”）。显式开启后后端缺能力（缺 ST 表 / 上市日期 / `pe_ttm` 列）时告警一次、该规则不生效（软回退）。
- `index_universe` / `factor_universe` 可用的指数代码取决于后端数据库已有的成分数据；后端无 `get_index_members` 能力或对应指数无数据时，告警一次、规则不生效（软回退）。
- `StockFilter` 是策略侧工具：引擎不过滤，策略在 `select()` 中自行调用 `self._filter.filter(bars, date_str)`。

### 4.5 `conditions` — 声明式离场条件

```yaml
conditions:
  stop_loss_pct: 0.06    # 跌 6% 止损 → STOP_LOSS at entry_price × (1 - 0.06)
  take_profit_pct: 0.25  # 涨 25% 止盈 → TAKE_PROFIT at entry_price × (1 + 0.25)
  trailing_pct: 0.08     # 从持仓期最高收盘价回撤 8% → TRAILING_TP at high × (1 - 0.08)
  model_exit:            # 持仓 bar 中 ml_<model> 分数 ≥ threshold → ML_EXIT（次日开盘成交）
    - {model: tb_guard, threshold: 0.6}
```

- 前三项必须是 `(0, 1)` 内的数值，否则加载时报错；未知 `conditions` 键直接 `ValueError`。
- `model_exit` 是 `[{model, threshold}]` 列表：`model` 必须在 `models` 节已声明（缺省 `threshold=0.5`，须 ∈ (0,1)）；引用未声明模型在加载时 `ValueError`。
- 所有规则由 `ConditionBuilder` 翻译为条件单 dict，基类默认 `calc_conditions` 已委托 `self._cond.calc()`（见 §3.4）；覆盖 `calc_conditions` 时自行 `self._cond.calc()` 取默认条件单再增删改。

### 4.6 `factor_library`

```yaml
factor_library: factors.yaml   # 相对于策略 YAML 所在目录
```

指向自定义因子库 YAML，覆盖默认 `factors/library.yaml`。详见 [因子库指南](./factor_library.md)。

### 4.7 自管理调仓 — 策略代码控制换手节奏

`select()` 每日运行，调仓节奏完全由策略代码自行控制：在 `select()` 中判断今天是否调仓，非调仓日返回空名单即可。引擎不提供声明式调仓调度，也不拦截 `select()` 的输出。

**时间门控模式**（固定间隔全量轮动，非调仓日可紧急卖出）：

```python
def on_start(self, provider, first_date, end_date=None):
    self._last_rebalance: int = 0
    self._rebalance_interval = int(self.config.get("rebalance_interval", 22))

def select(self, bars, account_snapshot, provider):
    if not bars:
        return {"buy": [], "sell": []}
    date_int = int(next(iter(bars.values())).get("trade_date", ""))
    current = set(account_snapshot.holdings.keys())

    # 非调仓日：只紧急卖出，不新买
    if date_int - self._last_rebalance < self._rebalance_interval:
        urgent = [s for s in current if self._needs_urgent_exit(s, bars.get(s, {}))]
        return {"buy": [], "sell": urgent}

    # 调仓日：完整轮动
    self._last_rebalance = date_int
    # ... 正常排名、选股、卖出逻辑
```

**排名阈值模式**（每只持仓独立跟踪，排名跌出阈值才卖出，换手由排名变化自然驱动）：见 `self_managed_rank` 示例。

| 对比维度 | 时间门控 | 排名阈值 |
|----------|----------|----------|
| 买侧触发 | 固定间隔 | 有空位即补 |
| 卖侧触发 | 非调仓日紧急卖 / 调仓日全量轮动 | 排名掉出阈值 + 持有够久 |
| 换手频率 | 由 `rebalance_interval` 决定 | 由排名变化速度自然决定 |

自管理让策略可以实现「卖随时、买定时」「调仓日只换 1-2 只」「广度差时跳过本次调仓」等非对称控制。注意：`bare_bones` / `rolling_ranker` 未做自管理，`select()` 每日全量轮动等同 day trading——精细化换手必须在代码中显式实现。示例：`self_managed_time`、`self_managed_rank`、`target_allocator`、`multi_model`。

---

## 5. 核心机制详解

### 5.1 select 返回协议

**模式一：买卖名单模式**（最常用）——返回 `buy` 和/或 `sell` 列表，可选附带 `buy_weights`、`sell_shares`、`buy_conditions`（symbol 与 buy/sell 不重叠）。

**模式二：目标市值模式**——返回 `target_value`。引擎计算每只股票目标市值与当前市值的差额，先卖出超配部分、再买入缺额部分（trigger = `"TARGET"`）。目标 = 0 → 清仓（零碎股一并卖出）；未出现在 dict 中的持仓不动。与 `buy`/`sell`/`buy_conditions` 同日互斥。

TARGET 加仓时按加权均价更新 `entry_price`，且整个持仓当日锁定（T+1）。

### 5.2 条件单系统

#### 5.2.1 声明式离场条件（ConditionBuilder）

```python
self._cond = ConditionBuilder(self.config.get("conditions", {}))
# calc_conditions 中委托：
return self._cond.calc(symbol, entry_price, bar, holding_days)
```

`ConditionBuilder` 将 YAML 声明的 `stop_loss_pct` / `take_profit_pct` / `trailing_pct` / `model_exit` 翻译为条件单 dict。`TRAILING_TP` 的持仓期最高收盘价由 `ConditionBuilder` 内部逐日跟踪，需定期用 `prune(live_symbols)` 清理已平仓标的的状态。

**内置离场类型触发规则**：

| 条件 | open 越过 | 否则 low/high 越过 | 否则 |
|------|-----------|-------------------|------|
| `STOP_LOSS` | `open <= price` → 按 open 成交 | `low <= price` → 按 price 成交 | 不触发 |
| `TAKE_PROFIT` | `open >= price` → 按 open 成交 | `high >= price` → 按 price 成交 | 不触发 |
| `TRAILING_TP` | 同 STOP_LOSS（price 由策略每日更新） | 同 STOP_LOSS | 不触发 |
| `ML_EXIT` | 次日开盘价直接成交（open 非法则顺延） | — | — |

条件单按**列表顺序**评估，首条触发生效即停止。一般顺序：止损 → 止盈 → 移动止盈。

#### 5.2.2 条件买入

`select()` 或 `on_tick()` 返回 `buy_conditions` 列表，每条 dict：

```python
{
    "symbol": "000001.SZ",
    "type": "LIMIT_BUY",          # 或 "BREAKOUT_BUY" 或自定义注册类型
    "price": 12.50,               # 触发价（必填，必须 > 0）
    "value": 20000.0,             # 买入金额（与 shares 二选一，恰填一个）
    # "shares": 2000,            # 精确股数
}
```

**内置买入类型**：

| 类型 | 触发逻辑 |
|------|----------|
| `LIMIT_BUY` | 限价回踩：`open <= price` → 按 open 成交；否则 `low <= price` → 按 price 成交 |
| `BREAKOUT_BUY` | 突破追涨：`open >= price` → 按 open 成交；否则 `high >= price` → 按 price 成交 |

条件买单 **T 日声明、T+1 日盘中触发、单日有效**，未触发自动失效。引擎自动执行的约束：

1. 已持有的 symbol 跳过（不重复入场）
2. 达到 `max_positions` 只记 INFO，不拦截
3. bar 缺失 / 价格非法 → 跳过
4. 涨停（`fill_price >= up_limit`）→ 跳过；涨跌停无法判定 → 跳过
5. `order_volume_ratio` 成交量 cap 生效；取整后不足 100 股 → 跳过
6. 现金不足 → 跳过该订单并告警（不缩股）
7. 股数取整到 100 股（1 手）的整数倍
8. 成交即 T+1 锁定

条件买入的滑点使用 `condition_slippage_ticks`（独立于手动买卖的 `slippage_ticks`）。

#### 5.2.3 自定义条件单 handler

**离场**：

```python
from btcore.match.conditions import register_condition_handler

def my_handler(holding, cond, bar):
    """
    holding: Holding 对象（entry_price, holding_days, shares 等）
    cond:    条件单 dict（calc_conditions 中传入的自定义键都在）
    bar:     当日 bar 数据
    返回: (executed: bool, fill_price: float, log_params: dict)
    """
    ...
    return (True, fill_price, {})

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

自定义 handler 返回的 `fill_price` 同样经过价格合法性、涨跌停、成交量 cap、现金检查等全部撮合护栏。

### 5.3 调仓节奏

引擎不提供声明式调仓调度。`select()` 每日运行，策略代码自行管理节奏；`on_tick` 和 `calc_conditions` 始终每日运行。详见 §4.7。

### 5.4 撮合与执行

**手动买卖**（`buy`/`sell` 名单，trigger = `"MANUAL"`）：

- 执行价 = `execution_price` 指定的次日开盘价或收盘价
- 等权分配：每只约 `执行时点总资产 / max_positions`，再受剩余现金约束（按剩余待买数均分）
- 加权分配：`buy_weights` 存在时每只买入 `min(total_value × weight, cash)`
- `sell_shares` 对指定 symbol 只卖出指定股数（受成交量 cap 截断），其余保留
- 买入只针对新标的（已在持仓中的 symbol 自动跳过）

**目标市值调仓**（`target_value`，trigger = `"TARGET"`）：先卖后买释放现金；target=0 清仓（零碎股一并卖出，卖出不要求整手）；加仓按加权均价更新成本并锁仓一天。

**通用护栏**（所有买卖路径）：

- **滑点**：买 +n tick、卖 −n tick（1 tick = 0.01 元）。成交记录的 `price` 已含滑点。
- **费用**：佣金（`max(成交额 × commission_rate, min_commission)`）+ 卖出印花税 + 过户费。
- **成交量 cap**：`order_volume_ratio` 配置时单笔股数 ≤ `int(vol(手) × ratio) × 100`。
- **价格非法**：成交价 None / NaN / 非正 → 跳过。条件单成交价非法 → 顺延（当日不再评估该持仓后续条件单）。
- **现金不足**：跳过该订单并告警，不缩股。
- **T+1 锁定**：买入当日 `Holding.locked = True`，次日解锁。锁定期间条件单自动跳过——不会当天买入当天止损卖出。
- **无当日行情**：标的停牌/缺数据（当日无 bar）→ 该标的当日所有订单跳过并告警（与涨跌停跳过同一告警通道）。卖出单不会顺延到次日重试——pending 每日由 `select()` 重算，平仓意图需策略在次日重新声明。
- **除息日停牌**：现金分红在除息日按持仓照常入账、成本相应扣减；但当日无 bar 时无法按 `pre_close` 做价格除权缩放（`entry_price`/条件单价格保持除权前口径）。复牌后市场价已除权，与持仓口径存在除权差，可能影响裸价收益计算或条件单触发——已知取舍：停牌期除权价不可观测，不引入启发式。
- **涨跌停**：
  - 涨停不买：`fill_price >= up_limit` → 买单跳过。
  - 跌停不卖：`fill_price <= down_limit` → 卖单跳过（条件单顺延）。
  - 涨跌停无法判定（`up_limit`/`down_limit` 缺失且无法由 `pre_close` 按板块规则推导）→ 买卖双双跳过。
  - 板块涨跌停幅度：主板 10%，创业板（300/301，2020-08-24 起）与科创板（688）20%，北交所 30%。

### 5.5 bar 数据契约

`select()` / `on_tick()` / `calc_conditions()` 收到的 bar dict 包含：

| 类别 | 列 | 说明 |
|---|---|---|
| 数据契约列（始终存在） | `open`, `high`, `low`, `close`, `vol`, `adj_factor`, `pre_close`, `up_limit`, `down_limit` | `vol` 单位为手（1 手 = 100 股） |
| 引擎派生列（始终存在） | `open_hfq`, `high_hfq`, `low_hfq`, `close_hfq`, `pct_chg` | `*_hfq = 裸价 × adj_factor`；`pct_chg` 由 `pre_close` 派生 |
| 因子列 | `FACTOR_SPECS` 引用的全部因子（含传递闭包） | 同名物化列 |
| 伪列（仅因子表达式引用时附着） | `industry`, `log_mktcap`, `idx_ret` | 行业分类 / log 总市值 / 基准日收益率 |
| ML 分数列 | `ml_<model>` | panel 模型物化列；holding 模型在决策时点注入持仓 bar（见 ml_guide） |
| 扩展字段 | 后端 `extra_fields` 登记的列（`pe_ttm`、`turnover_rate` 等） | 需经 `REQUIRED_FIELDS` 声明保留 |

**前视保护**：`select()` / `on_tick()` 中经 `provider.get_historical_bars()` 查询历史数据时，引擎钳制查询端到当前模拟日之前——策略传未来日期也拿不到未来数据。钳制从 preload 阶段（`get_universe` / `on_start`）即生效：钩子内查询同样以首日前一交易日为锚。

---

## 6. 从简单到复杂：逐级教程

每级基于 `strategies/examples/` 中的真实代码。建议按序阅读示例源码。

### 6.1 Level 0：裸因子轮动 — `strategies/examples/bare_bones/`

**目标**：最简策略完整骨架。基类默认接线（`filter_rules` → `filter_bars()` 过滤，`conditions` → 默认 `calc_conditions` 条件单）+ `eval_factor_specs` 打分。代码即 §1 快速开始的形态，无 `on_fills`、无 `buy_weights`，每日全量轮动。

### 6.2 Level 1：进阶轮动 — `strategies/examples/rolling_ranker/`

**新增能力**：`on_fills` 成交感知 + `on_tick` 每日维护 + `buy_weights` 加权分配 + `holding_days` 动态调参 + `REQUIRED_FIELDS` 声明。

关键模式：

```python
def on_fills(self, trades, provider):
    """条件单退出的标的进入冷却期。"""
    for t in trades:
        if t.side == "SELL" and t.trigger in ("STOP_LOSS", "TAKE_PROFIT", "TRAILING_TP"):
            self._cooldown[t.symbol] = int(t.date) + self._cooldown_days

def on_tick(self, bars, snapshot, provider):
    """冷却期递减 + 条件单状态修剪。"""
    expired = [s for s, d in self._cooldown.items() if d <= date_int]
    for s in expired:
        del self._cooldown[s]
    self._cond.prune(set(snapshot.holdings.keys()))

def calc_conditions(self, symbol, entry_price, bar, holding_days):
    conds = self._cond.calc(symbol, entry_price, bar, holding_days)
    for c in conds:
        if c.get("type") == "STOP_LOSS":
            if holding_days <= 3:
                c["price"] = entry_price * 0.97   # 新仓紧止损
            elif holding_days > 30:
                c["price"] = entry_price * 0.85   # 老仓放宽
    return conds
```

`buy_weights` 按得分比例分配，总和 < 1 保留现金缓冲。

### 6.2.1 岔路：自管理换手 — `self_managed_time/` · `self_managed_rank/`

两种模式见 §4.7。`self_managed_time`：时间门控 + 非对称买卖（买侧固定间隔，卖侧随时）；`self_managed_rank`：排名阈值 + 逐仓独立管理（`on_fills` 记录入场日期，排名掉出 `top_k × sell_rank_mult` 且持有 ≥ `min_hold_days` 才卖），并演示 `provider.get_historical_bars()` 历史回溯。

### 6.3 Level 2：目标仓位调仓 — `strategies/examples/target_allocator/`

**新增能力**：`target_value` 精确仓位管理 + 近边缘持仓减半 + 时间门控自管理 + `materialize_only` 因子。

关键模式：

```python
# 不在 top_k 的持仓：target = 0（清仓）；在 top_k 的按得分比例分配
target_value = {}
for sym in current:
    if sym not in top_symbols.index:
        target_value[sym] = 0.0
raw_w = top_symbols.clip(lower=0)
w_sum = raw_w.sum()
if w_sum > 0:
    for sym in top_symbols.index:
        target_value[sym] = float(allocable * raw_w[sym] / w_sum)

# 近边缘持仓（top_k × 1.5 内）以半价市值作为 target，减半而非清仓
near_top = set(sorted_score.head(int(self._top_k * 1.5)).index)
for sym in list(target_value):
    if target_value[sym] == 0.0 and sym in near_top:
        h = account_snapshot.holdings.get(sym)
        if h and h.shares >= 200:
            target_value[sym] = h.last_price * h.shares * 0.5

return {"buy": [], "sell": [], "target_value": target_value}
```

配套配置：`execution_price: "close"`、`condition_slippage_ticks`、`benchmark` 显式覆盖、`pct_above_ma20` 以 `materialize_only: true` 物化供 `calc_conditions` 读取。

### 6.4 Level 3：条件单猎手 — `strategies/examples/condition_hunter/`

**新增能力**：`buy_conditions` 条件买入 + 自定义离场/入场 handler + `on_tick` 返回条件买单。

架构：主仓 top_k 走 `buy` 名单；紧随其后的 `hunt_count` 只备选标的挂条件买单（`LIMIT_BUY` 回踩 + `BREAKOUT_BUY` 突破 + 自定义 `VWAP_BUY` 均价，每只约 2% 试探仓）；标准止损被自定义 `DYNAMIC_STOP`（波动率自适应）替代。`on_start` 中注册两个自定义 handler。配套配置：`condition_slippage_ticks: 1`、`order_volume_ratio: 0.05`。

### 6.5 Level 4：状态机多模型策略 — `strategies/examples/multi_model/`

**新增能力**：自定义因子库 + 市场状态机 + 多模型投票 + 交易域/因子计算域分离 + 全部 hook 协同。

架构要点：

- `factor_library: factors.yaml` 指向同目录自定义因子库；坍缩算子（`mean(close_hfq > ma(close_hfq, 20))`）构建全市场广度因子，以 `materialize_only: true` 物化供状态机读取。
- `index_universe`（沪深 300，交易域）与 `factor_universe`（中证 800，因子计算域）分离，截面/坍缩口径更宽。
- 3 套子模型因子规格（动量/反转/质量）在 `select()` 中分别 `eval_factor_specs` 打分，按市场态（bull/neutral/bear）动态加权合成；仓位乘数调整 `top_k`。
- 市场状态机在 `on_tick` 中每日推进（广度均值 + 连续 N 日确认切换），不受调仓节奏限制。
- `on_fills` 精确跟踪持仓状态（加权均价、最高价、入场日期、退出 trigger），正确处理加仓场景。
- 4 层条件单：内置三件套 + `DYNAMIC_STOP`（波动率自适应）+ `TIME_STOP`（60 日强制退出）。

---

## 7. 进阶模式与技巧

### 7.1 多模型投票/集成

1. `on_start` 中定义 N 套 `factor_specs`（每套 `[{name, weight, ascending}]`）
2. `select` 中分别 `eval_factor_specs` 获取各模型得分
3. 静态或动态权重加权合成；动态权重由 `on_tick` 的市场状态检测驱动

```python
scores = {}
for name, specs in self._models.items():
    _, scores[name] = eval_factor_specs(df, specs)
final = sum(scores[n].fillna(0) * self._model_weights[n] for n in scores)
```

注意：所有子模型引用的因子都必须出现在 YAML `factor_specs` 中（可用 `materialize_only`），否则引擎不物化。

### 7.2 市场状态检测

坍缩算子（`mean` / `group_mean`）构建市场广度因子——在全市场聚合后广播回个股（机制见 [因子库指南](./factor_library.md)）：

```yaml
factors:
  mkt_breadth20:
    expr: "mean(close_hfq > ma(close_hfq, 20))"     # 全市场站上 MA20 占比
  industry_strength:
    expr: "group_mean(roc(close_hfq, 20), industry)" # 行业平均动量（map 回个股）
```

策略在 `on_tick` 中读取这些因子驱动状态机；`on_tick` 每日运行使状态检测不受调仓节奏限制。

### 7.3 冷却期管理

- **精确模式**：`on_fills` 按 `Trade.trigger` 感知条件单退出 → 记入冷却 map（可对 STOP_LOSS 加倍冷却）。
- **免 hook 模式**：`on_tick` 中对比持仓变化递减。
- `select` 中排除：`score = score[~score.index.isin(self._cooldown)]`。

### 7.4 动态参数调整

- **持仓天数自适应**：`calc_conditions` 中 `if holding_days <= N` 收紧止损/不给止盈。
- **市场态仓位乘数**：牛满仓 / 震荡 85% / 熊 60%，调整 `top_k` 和 `buy_weights`。
- **波动率自适应**：从 `bar["pct_chg"]`、`bar["turnover_rate"]` 估算波动，动态调整止损宽度。
- **市场广度自适应**：广度差时收紧选股条件或跳过调仓。

### 7.5 部分减仓

`sell_shares` 用于「持有但减仓」——不在 top_k 但排名尚可的持仓减半保留，降低换手：

```python
near_top = set(sorted_score.head(int(self._top_k * 1.3)).index)
for sym in current - target:
    if sym in near_top and account_snapshot.holdings[sym].shares >= 200:
        sell_shares[sym] = account_snapshot.holdings[sym].shares // 2
```

对比：`target_value` 做精确市值管理（增减到目标值），`sell_shares` 做简单减仓（固定股数）。两者同日不可混用（`sell_shares` 属于 buy/sell 模式）。

### 7.6 条件买入策略

`buy_conditions` 与 `buy` 名单的典型配合：主仓 buy 名单持有 top_k；备选标的挂小额（1-2%）条件买单试探；同一标的可挂多种类型（LIMIT_BUY 回踩 + BREAKOUT_BUY 突破）增加成交概率。条件买单单日有效、未触发自动失效，symbol 不得与 buy/sell 名单重叠。非调仓日的条件买单经 `on_tick` 返回。

### 7.7 列裁剪与性能

- `REQUIRED_FIELDS` 只声明 `select()`/`on_tick()` 中命令式访问的扩展列；因子列与基础列自动覆盖。
- 不声明而访问 → `KeyError`。
- `get_universe()` / `filter_rules.index_universe` 裁剪 preload 数据量；`factor_universe` 让因子计算域宽于交易域，提升截面算子口径质量。

---

## 8. 反模式与常见错误

| 错误 | 后果 | 修复 |
|------|------|------|
| 访问未在 `REQUIRED_FIELDS` 声明的列 | `KeyError` | 列名加入 `REQUIRED_FIELDS` |
| 忘记 `register_condition_handler` 就使用自定义 type | 决策时点 `ValueError: 未注册的条件单类型` | 在 `on_start` 中注册 |
| `buy`/`sell` 名单含重复 symbol | 决策时点 `ValueError`（重复买入会双重扣款+持仓覆盖） | 名单去重 |
| `select` 返回键名 typo（如 `buy_condition`） | 决策时点 WARNING，该键静默忽略 | 对照 §3.3 键表检查 |
| 条件单缺必填键（内置类型缺 `price`） | 决策时点 `ValueError` | 内置类型必须给数值 price |
| `target_value` 与 `buy`/`sell`/`buy_conditions` 同日混用 | `ValueError` | 二选一 |
| `sell_shares` 包含不在 `sell` 中的 symbol | `ValueError` | 确保键是 `sell` 子集 |
| `buy_weights` 键与 `buy` 列表不一致 / 权重和 > 1 | `ValueError` | 键精确匹配，权重和 ≤ 1 |
| `buy_conditions` 的 symbol 与 buy/sell 名单重叠 | `ValueError` | 条件买单只挂名单外标的 |
| 条件买单同时填 `value` 和 `shares`，或都不填 | `ValueError` | 恰填一个 |
| 覆盖 `on_tick` 却忘记 `ConditionBuilder.prune()` | trailing 状态泄漏到已平仓标的 | 调用 `super().on_tick(...)` 或自行 prune |
| 覆盖 `on_start` 却忘记 `super().on_start(...)`（且声明了 filter_rules） | `filter_bars()` 直接报错 | on_start 先调 super |
| 条件单 `price` 填 `None` 但自定义 handler 未计算价格 | handler 收到 None | 内置类型必须给数值 price；自定义 handler 自行计算 |
| 把每日状态更新写在 `select()` 的调仓 early-return 之后 | 非调仓日状态不更新 | 状态更新移到 `on_tick` |
| 指望 `max_positions` 拦截超额买入 | 引擎只记 INFO 不拦截 | 策略自行控制 buy 名单长度 |
| 未显式声明 `exclude_loss: true` | `pe_ttm` 不 preload，亏损过滤不生效 | 需要时才显式声明（未声明 = 不过滤，不再误告警） |
| 空 `bars` 时未提前返回 | `StopIteration` / `KeyError` | `if not bars: return {"buy": [], "sell": []}` |
| 用裸价（`close`）做排名/信号 | 除权日跳空产生虚假信号 | 因子侧用 `*_hfq` 列 |

---

## 9. 运行与调试

### 9.1 CLI 运行

```bash
# 单次回测（--out 缺省 :memory: 不落盘；缺省自动生成 HTML 报告到策略目录 reports/ 下）
python scripts/run.py strategies/my_strategy/config.yaml --start 20240101 --end 20240630

# 指定结果库 / 覆盖初始资金 / 关闭报告
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out result.db --capital 500000 --no-report
```

### 9.2 查看结果

```bash
# 从结果库生成单 run HTML 报告（--run-id 缺省取最新）
python scripts/report.py result.db --out report.html

# 多 run 对比（终端表格 + 可选对比 HTML）
python scripts/compare.py result.db --runs 1,2,3 --html compare.html

# 交叉验证（交易合理性 / 异常检测 / 小资金磨损检查）
python scripts/cross_validate.py result.db --strategy my_strategy --run-id 1
```

### 9.3 程序化调用

```python
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy

strategy = load_strategy("strategies/my_strategy/config.yaml")
provider = DataProvider(backend)
engine = Engine(strategy, provider, db_path="result.db", debug=False)
# Engine(strategy, provider, initial_capital=None, db_path=None,
#        max_positions=None, debug=False) —— initial_capital/max_positions 覆盖 YAML

result = engine.run("20240101", "20240630")
# result["account_daily"]   → DataFrame，每日账户快照
# result["trade_log"]       → DataFrame，所有成交
# result["statistics"]      → dict，统计指标（见下）
# result["benchmark_nav"]   → list[float] | None，基准净值序列
# result["benchmark_code"]  → str | None，基准代码
```

`result["statistics"]` 的关键指标组：

| 指标组 | 包含 | 说明 |
|--------|------|------|
| 收益风险 | `total_return`、`annualized_return`、`sharpe`、`sortino`、`max_drawdown`、`calmar`、`monthly_returns`、`yearly_returns` | 标准绩效指标 |
| 交易磨损 | `trading_friction` — 双边磨损率、年化拖累、成本占盈利比、无摩擦对照收益 | 交易成本对收益的侵蚀 |
| 持仓复杂度 | `management_complexity` — 单日最大成交笔数、有成交天数占比、单票平均市值 | 手动跟单的可执行性 |
| 卖出来源 | `sell_source` — 按卖出 trigger（MANUAL / TARGET / 条件单类型）分组的笔数与盈亏 | 退出行为构成 |

同一 `db_path` 多次 `run()` 按 `run_id` 增量追加；run 中抛异常时该 run 的 `status` 改写为 `failed`。

程序化构建策略（无需 YAML 文件）可用 `btcore.strategy_loader.build_strategy(cls, config, factor_specs=..., filter_rules=...)`，与 `load_strategy` 行为等价。

### 9.4 Debug 模式

`Engine(debug=True)` 时引擎每日写入一条 `debug_snapshots` 记录到结果库（需 `db_path` 落盘），`snapshot_json` 包含：

- `account`：当日现金、总资产、持仓数
- `pending`：当日买卖名单与条件买单
- `holdings_detail`：每只持仓的股数、入场价、入场日期、持仓天数
- `bars_subset`：涉及标的（持仓 + 买入目标 + 条件买单）的当日行情截面（含因子列值）

配合 `scripts/replay.py` 按 symbol/日期回放完整决策上下文，定位「某天为什么买/卖了某只股票」：

```bash
python scripts/replay.py result.db --run-id 1 --symbol 000001.SZ --date 20240315
```

### 9.5 调试技巧

- 策略内用 `logging.getLogger(__name__)` 输出调试信息；`quiet_skips: true` 可降噪跳过类告警。
- `snapshot.trades` 在 `select()` 中读取当日成交（与 `on_fills` 同一份数据）。
- `provider.get_historical_bars()` 前视保护自动生效，无需手动裁剪日期。
- 自定义条件单类型未注册会在决策时点（而非次日撮合）报错——报错信息列出全部已注册类型。

---

## 10. 参考速查表

### 10.1 select 返回键一览

| 键 | 类型 | 与 target_value 互斥 | 说明 |
|---|---|---|---|
| `buy` | `list[str]` | 是 | 买入名单 |
| `sell` | `list[str]` | 是 | 卖出名单（默认全清） |
| `buy_weights` | `dict[str, float] \| None` | 是 | 买入权重，键 = buy，单项 ∈ (0,1]，和 ≤ 1 |
| `sell_shares` | `dict[str, int] \| None` | 是 | 部分减仓股数，键 ⊆ sell，正整数 |
| `buy_conditions` | `list[dict] \| None` | 是 | 条件买单，symbol 不得与 buy/sell 重叠 |
| `target_value` | `dict[str, float] \| None` | — | 目标市值，0 = 清仓 |

### 10.2 YAML 全部键一览

```
顶层: name, strategy*, config, factor_specs, filter_rules, conditions,
      factor_library, models
config: initial_capital, max_positions, slippage_ticks, condition_slippage_ticks,
        execution_price, commission_rate, min_commission, stamp_tax_rate,
        transfer_fee_rate, benchmark, quiet_skips, order_volume_ratio, ml_log
        + 用户自定义键
factor_specs 条目: factor*, weight, ascending, materialize_only
filter_rules: exclude_st, exclude_new_stock, exclude_loss, exclude_boards,
              exclude_industries, min_price, index_universe, factor_universe
conditions: stop_loss_pct, take_profit_pct, trailing_pct, model_exit
```

### 10.3 条件单类型一览

| 类别 | type 值 | 注册方式 | 触发方向 |
|------|---------|---------|---------|
| 买入 | `LIMIT_BUY` | 内置 | 低吸回踩 |
| 买入 | `BREAKOUT_BUY` | 内置 | 突破追涨 |
| 买入 | 自定义 | `register_buy_condition_handler(type, handler)` | 自定义 |
| 卖出 | `STOP_LOSS` | 内置 | 固定止损 |
| 卖出 | `TAKE_PROFIT` | 内置 | 固定止盈 |
| 卖出 | `TRAILING_TP` | 内置 | 移动止盈 |
| 卖出 | `ML_EXIT` | 声明 `models` 时自动注册 | 次日开盘离场 |
| 卖出 | 自定义 | `register_condition_handler(type, handler)` | 自定义 |

handler 签名：

```python
# 离场 → (executed, fill_price, log_params)
def handler(holding: Holding, cond: dict, bar: dict) -> tuple[bool, float, dict]
# 入场 → (executed, fill_price, log_params)
def handler(order: dict, bar: dict) -> tuple[bool, float, dict]
```

### 10.4 Trade.trigger 值一览

| trigger | 含义 | 来源 |
|---------|------|------|
| `"MANUAL"` | 手动买卖 | `select()` 返回的 buy/sell |
| `"TARGET"` | 目标市值调仓 | `select()` 返回的 target_value |
| `"STOP_LOSS"` | 固定止损 | `conditions.stop_loss_pct` |
| `"TAKE_PROFIT"` | 固定止盈 | `conditions.take_profit_pct` |
| `"TRAILING_TP"` | 移动止盈 | `conditions.trailing_pct` |
| `"ML_EXIT"` | ML 模型离场 | `conditions.model_exit` |
| `"CORPORATE"` | 现金分红入账 | 公司行为（side = `"DIV"`） |
| 自定义 | 自定义条件 | `register_*_condition_handler` 注册的 type |

### 10.5 Holding 属性一览

| 属性 | 类型 | 说明 |
|------|------|------|
| `symbol` | `str` | 股票代码 |
| `shares` | `int` | 持仓股数 |
| `entry_date` | `str` | 入场日期 (YYYYMMDD) |
| `entry_price` | `float` | 入场均价（公司行为调整后；加仓按加权均价更新） |
| `cost` | `float` | 持仓总成本 |
| `last_price` | `float` | 最新市价 |
| `holding_days` | `int` | 持仓天数 |
| `conditions` | `list[dict]` | 当日计算的条件单列表（引擎附着） |
| `locked` | `bool` | T+1 锁定（买入当日不可卖出） |

`snapshot.holdings` 是深拷贝，修改不影响引擎状态。

### 10.6 引擎工具函数一览

```python
from btcore.strategy_tools import bars_to_df, eval_factor_specs, ConditionBuilder
from btcore.filters import StockFilter
from btcore.match.conditions import register_condition_handler, register_buy_condition_handler

# bars_to_df(bars: dict[str, dict]) -> pd.DataFrame
#   dict-of-dicts → symbol 索引 DataFrame（空输入 → 空 DataFrame）

# eval_factor_specs(df: DataFrame, specs: list[dict]) -> tuple[DataFrame, Series]
#   读物化因子列 → (factor_df, composite_score)
#   spec: {name, weight=1.0, ascending=False, materialize_only=False}
#   非 materialize_only 因子先转截面 percentile rank 再加权平均，score ∈ [0,1]
#   specs 为空时 score 全 1.0；materialize_only 条目只在 factor_df 中出现

# ConditionBuilder(rules: dict)
#   .calc(symbol, entry_price, bar, holding_days) -> list[dict]  # 规则全空 → []
#   .prune(live_symbols) -> None  # 清理已平仓标的的 trailing 状态

# StockFilter(backend, start_date, rules, end_date=None)
#   .filter(bars: dict, date_str: str) -> dict  # 返回过滤后的 bars

# register_condition_handler(type: str, handler) -> None      # 进程级全局
# register_buy_condition_handler(type: str, handler) -> None  # 进程级全局
```

### 10.7 结果库 SQLite Schema

回测结果落盘 SQLite。同一 `db_path` 多次 `engine.run()` 按 `run_id` 增量追加。

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

**account_daily**（逐日账户快照，PK = (run_id, date)）：

| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` / `date` | INTEGER / TEXT | 关联 runs；交易日 |
| `cash` | REAL | 可用现金 |
| `total_value` | REAL | 总资产 |
| `daily_pnl` / `cumulative_pnl` | REAL | 当日 / 累计盈亏 |
| `initial_capital` | REAL | 初始资金 |
| `n_holdings` | INTEGER | 持仓数 |

**trade_log**（逐笔成交记录）：

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | 记录编号 |
| `run_id` / `date` / `symbol` | — | 关联 runs；成交日期；股票代码 |
| `side` | TEXT | `BUY` / `SELL` / `DIV`（公司行为） |
| `trigger` | TEXT | 触发类型（见 §10.4） |
| `price` | REAL | 成交价（已含滑点） |
| `shares` | INTEGER | 成交股数 |
| `turnover` | REAL | 成交金额（未含滑点） |
| `commission` / `stamp_tax` / `transfer_fee` | REAL | 佣金 / 印花税（仅卖出）/ 过户费 |
| `slippage_amount` | REAL | 滑点金额 |
| `net_amount` | REAL | 净现金流 |
| `reason` | TEXT | 备注 |

**debug_snapshots**（仅 `Engine(debug=True)` 写入，PK = (run_id, date)）：`run_id` / `date` / `snapshot_json`（account/pending/holdings_detail/bars_subset）。

**其余表**：`holdings`（瞬态持仓快照，每次 run 开始清空，含 `conditions_json`）；`ml_predictions`（ML 分数落盘，PK = (run_id, date, symbol, model)，见 ml_guide）。

---

## 11. 与现有示例的对照

| 示例 | 目录 | 展示的核心能力 |
|------|------|---------------|
| bare_bones | `strategies/examples/bare_bones/` | 最简骨架：StockFilter + eval + ConditionBuilder |
| rolling_ranker | `strategies/examples/rolling_ranker/` | on_fills + on_tick + buy_weights + 动态参数 |
| self_managed_time | `strategies/examples/self_managed_time/` | 时间门控 + 非对称买卖 |
| self_managed_rank | `strategies/examples/self_managed_rank/` | 排名阈值 + 逐仓独立管理 + 历史回溯 |
| target_allocator | `strategies/examples/target_allocator/` | target_value + 时间门控 + 近边缘减半 + materialize_only |
| condition_hunter | `strategies/examples/condition_hunter/` | buy_conditions + 自定义 handler + on_tick 条件买单 |
| multi_model | `strategies/examples/multi_model/` | 全部能力：状态机 + 多模型 + 坍缩因子 + 域分离 |

建议按此顺序阅读源码：先理解 bare_bones 的基本骨架，再逐级叠加新能力，最后看 multi_model 的全景。
