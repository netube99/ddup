# 策略接口与执行语义

策略由 YAML 配置和 Python Strategy 子类组成。子类只必须实现 select；引擎负责
数据准备、撮合、公司行为与落库。[五要素与示例](strategy_patterns.md)用于选实现模式，
本文用于查字段与边界；运行命令见[CLI](cli_and_research.md)，结果结构见[分析指南](results_analysis.md)。

[钩子](#hooks) · [select 协议](#select) · [YAML](#yaml) · [引擎参数](#config) ·
[过滤](#filters) · [条件单](#conditions) · [撮合](#execution) · [数据查询](#bars) · [Python 调用](#python-api)

最小可运行示例：[bare_bones 配置](../strategies/examples/bare_bones/config.yaml)与
[代码](../strategies/examples/bare_bones/strategy.py)。需要真实行情与本机因子库。

<a id="hooks"></a>
## 生命周期钩子

<a id="declarations"></a>
### 声明属性

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

### `on_start(self, provider, first_date, end_date=None)`

**可选覆盖。** 基类默认实现：`FILTER_RULES` 非空时构建 `StockFilter` 挂到 `self._filter`。回测开始前调用一次。覆盖时在此初始化所有策略状态：

- 先调用 `super().on_start(provider, first_date, end_date)` 以构建 `self._filter`
- 注册自定义条件单 handler（见 [自定义 handler](#custom-conditions)）
- 解析自定义 config 参数：`self._top_k = int(self.config.get("top_k", 5))`
- 初始化状态字典：冷却期 map、持仓跟踪 dict、市场状态变量

> 覆盖但忘记 `super().on_start(...)` 时，`filter_bars()` 会在 `FILTER_RULES` 非空时直接报错（fail-fast），而不是让过滤规则静默失效。

<a id="select"></a>
### `select(self, bars, account_snapshot, provider)` → dict

**必须实现。** 有 bar 的决策日调用，首日前一交易日也预跑；加载池整日无 bar 时不调用。参数：

- `bars`: `dict[str, dict]` — 当日截面数据，键为 symbol，值为 OHLCV + 因子列 + 扩展字段的 dict。引擎保证 `trade_date` 字段存在。
- `account_snapshot`: `Snapshot` 对象：
  - `cash` — 可用现金
  - `holdings` — `dict[str, Holding]`，当前持仓的**深拷贝**（修改不影响引擎状态）
  - `trades` — 当日已执行的成交列表 `list[Trade]`（与 `on_fills` 收到的同一份数据）
  - `total_value` — 总资产（现金 + 持仓市值）
- `provider`: `DataProvider` 对象，提供 `get_historical_bars(symbols, end_date, lookback_days=365)`、`get_benchmark_returns(end_date, lookback_days=252)`、`get_benchmark_trend(end_date, window=30)`（基准近 window 日累计收益）等查询（均带前视保护，见 [bar 与查询](#bars)）。

**返回值**支持的键：

| 键 | 类型 | 语义 |
|---|---|---|
| `buy` | `list[str]` | 买入名单。空列表 = 无买入。 |
| `sell` | `list[str]` | 卖出名单（默认全部清仓）。空列表 = 无卖出。 |
| `buy_weights` | `dict[str, float] \| None` | 每只买入的资金权重，每项 ∈ (0, 1]，权重和 ≤ 1。引擎按 `total_value × weight` 分配资金。键必须与 `buy` 列表精确一致。`None` = 等权买入。 |
| `sell_shares` | `dict[str, int] \| None` | 部分减仓股数（正整数）。键必须是 `sell` 列表的子集。 |
| `sell_reasons` | `dict[str, str] \| None` | 按 symbol 覆盖手动卖出的 trigger（缺省 `"MANUAL"`），值非空字符串、键 ⊆ sell。用于卖出来源归因（如 TREND_BREAK 盘前评估离场）；`on_fills` 冷却期记账与 ML holding 标签都消费 trigger。 |
| `buy_conditions` | `list[dict] \| None` | T+1 日盘中条件买单列表，格式见 [条件买入](#buy-conditions)。 |
| `target_value` | `dict[str, float] \| None` | 每只股票的目标市值，引擎自动计算买卖差额。`0` = 清仓；未出现的 symbol 不动。键必须是非空字符串、值必须 ≥ 0 的有限数值，否则 `ValueError`。与 `buy`/`sell`/`buy_conditions` 同日互斥。 |

**冲突校验**（引擎在决策时点执行，违规即 `ValueError`，fast-fail）：

| 违规 | 错误 |
|---|---|
| `buy` 与 `sell` 有交集 | `ValueError: 同日买卖冲突` |
| `buy`/`sell` 名单含非字符串或空字符串元素 | `ValueError` |
| `target_value` 与 `buy`/`sell` 同时非空 | `ValueError: 互斥` |
| `target_value` 与 `buy_conditions` 同时非空 | `ValueError: 互斥` |
| `buy_weights` 的键与 `buy` 列表不一致，或权重 ∉ (0,1]，或权重和 > 1 | `ValueError` |
| `sell_shares` 的键不在 `sell` 列表中，或值不是正整数或超过当前持仓股数 | `ValueError` |
| `sell_reasons` 的键不在 `sell` 列表中，或值不是非空字符串 | `ValueError` |
| `buy_conditions` 的 symbol 出现在 `buy` 或 `sell` 名单中 | `ValueError` |
| 条件买单缺 `symbol`/`type`/`price`、`price` 非正数、`value` 与 `shares` 同时填或同时缺 | `ValueError` |
| 条件单/条件买单的 `type` 未注册 | `ValueError: 未注册的条件单类型` |

注意：`buy` 名单与 `buy_conditions` **可以同日返回**，但 symbol 不得重叠（典型用法：主仓 buy 名单 + 备选标的挂条件买单，见 `condition_hunter` 示例）。

**空返回** `{"buy": [], "sell": []}` 完全合法——今日不做任何操作。

### `calc_conditions(self, symbol, entry_price, bar, holding_days)` → list[dict]

**可选覆盖。** 基类默认实现委托 `ConditionBuilder.calc()`（把 YAML `conditions` 节翻译为条件单）。引擎对**每个持仓每日**调用。返回条件单 dict 列表；空列表 = 该持仓无条件单。

- `symbol` — 股票代码
- `entry_price` — 入场均价（公司行为调整后）
- `bar` — 该 symbol 当日 bar dict
- `holding_days` — 持仓天数（含本日）

每条条件单至少包含 `type`（str）；其余键由对应 handler 自行定义（内置止损/止盈 handler 消费 `price`，`ML_EXIT` 消费 `model`/`score`，自定义 handler 可定义任意键）。引擎按**列表顺序**评估，首条触发生效后不再检查后续条件。返回的 `type` 在决策时点即校验——未注册的类型或缺必填键（如内置类型的 `price`）当天就报错，不会拖到次日撮合。

覆盖扩展：先 `conds = self._cond.calc(symbol, entry_price, bar, holding_days)` 拿默认条件单，再增删改（如 `holding_days` 自适应、替换 `STOP_LOSS`）。

### `on_fills(self, trades, provider)`

**可选 hook。** 引擎在 `select` 之前调用，传入当日实际成交的 `Trade` 列表，无成交时为空列表。回测首日前的预跑也会以空列表调用一次。

`Trade` 属性：

| 属性 | 类型 | 说明 |
|------|------|------|
| `date` | `str` | 成交日期 (YYYYMMDD) |
| `symbol` | `str` | 股票代码 |
| `side` | `str` | `"BUY"` 或 `"SELL"` |
| `trigger` | `str` | 触发类型（见 [结果事件](results_analysis.md#events)） |
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

### `on_tick(self, bars, snapshot, provider)` → dict | None

**可选 hook。** 有 bar 的决策日调用，在 `on_fills` 之后、`select` 之前。

基类实现不是空函数：它调用 `ConditionBuilder.prune(当前持仓标的集合)` 清理已平仓标的的 trailing 锚点，然后返回 `None`。覆盖时**必须**显式调用 `super().on_tick(bars, snapshot, provider)`，否则重入场标的会沿用旧锚点，止盈止损基准被污染。

典型用途：
- 冷却期递减计数
- 市场状态机推进（广度检测 → 状态切换）
- 逐仓最高价跟踪

**返回值**：可返回 `{"buy_conditions": [{symbol, type, price, value|shares}]}`，引擎将其合并进 `select()` 返回的 `buy_conditions`（合并后统一走 [select 协议](#select) 的校验）。策略因此可以在非调仓日提交条件买单（如突破买入），不受调仓窗口限制。**返回值只支持 `buy_conditions` 一个键**——返回 `buy`/`sell`/`target_value` 等其他键会直接 `ValueError`（买卖名单只能经 select 返回）：

```python
def on_tick(self, bars, snapshot, provider):
    super().on_tick(bars, snapshot, provider)
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

### `get_universe(self, provider, start, end)` → list[str] | None

**可选覆盖。** 返回策略交易域的股票符号列表；基类返回 `None`（全市场）。引擎 preload 阶段调用，裁剪数据加载范围。

若 YAML `filter_rules` 配置了 `index_universe` 且策略未覆盖此方法，loader 自动生成实现（多指数成分区间并集）。手动覆盖后 `index_universe` 的自动裁剪不再生效（但 StockFilter 的逐日成分过滤仍生效）。

### `get_factor_universe(self, provider, start, end)` → list[str] | None

**可选覆盖。** 返回因子计算域的股票符号列表，可以宽于交易域；基类返回 `None`（因子计算域 = 交易域）。`filter_rules.factor_universe` 配置且未覆盖时 loader 自动生成。

### 类变量 `REQUIRED_FIELDS`

声明 `select()` / `on_tick()` 中**命令式访问**的扩展列（`bar["col"]` 形式）。引擎 preload 列裁剪时保留这些列。不需要声明：

- 因子列（`FACTOR_SPECS` / `FACTOR_NODES` 自动覆盖）
- 基础列（`open`/`high`/`low`/`close`/`vol`/`adj_factor` 永不裁剪）

策略访问了 `bar["turnover_rate"]`、`bar["pe_ttm"]`、`bar["amount"]` 这类扩展字段时必须声明，否则 `KeyError`。

```python
REQUIRED_FIELDS: list[str] = ["turnover_rate"]
```

### 类变量 `CONDITION_FACTORS`

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

<a id="yaml"></a>
## YAML 配置

### 顶层键

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

<a id="config"></a>
### config 引擎参数

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
| `order_volume_ratio` | `float \| None` | `None` | 单笔订单股数 ≤ `int(成交量(手) × ratio) × 100`；`None` 不限制。必须是 (0,1] 内数值或 None，否则 `ValueError`（>1 可超当日成交量） |
| `ml_log` | `str` | — | `"full"` 时落盘全截面 ML 分数（缺省只落盘决策相关标的），见 ml_guide |

**自定义键**：任何不在此列表的键（`top_k`、`cooldown_days`、`rebalance_interval` 等）策略代码通过 `self.config.get("key")` 自由读取，引擎不干预。

<a id="factors"></a>
### factor_specs

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

<a id="filters"></a>
### filter_rules

| 键 | 类型 | 默认 | 需要后端能力 | 说明 |
|---|---|---|---|---|
| `exclude_st` | `bool` | `False` | `st_symbol` | 排除 ST（日频快照：当日有记录才是 ST，摘帽次日恢复可买）。未声明 = 不过滤 |
| `exclude_new_stock` | `bool` | `False` | `listing_date` | 固定排除 start 前 60 日历日至 end 内上市的预加载名单；非逐日年龄过滤 |
| `exclude_loss` | `bool` | `False` | `eps` 列 | 排除亏损股（`eps < 0`；该行无 `eps` 时退回 `pe_ttm <= 0`）。未声明 = 不过滤。注意：不能假设数据源总会用负 PE 标记亏损；`eps` 缺失时 preload 汇总告警一次 |
| `exclude_boards` | `list[str]` | `[]` | — | 排除板块：`"BJ"`（北交所）、`"688"`（科创板）、`"300"`/`"301"`（创业板） |
| `exclude_industries` | `list[str]` | `[]` | `industry_name` | 排除指定行业名 |
| `min_price` | `float` | `0.0` | — | 最低收盘价过滤 |
| `index_universe` | `list[str]` | — | `index_code` + `index_member` | 仅交易指数成分股（多指数并集）。白名单只管入场，持仓被调出指数不强制卖出 |
| `factor_universe` | `list[str]` | — | `index_code` + `index_member` | 因子计算域（可宽于交易域）。不填 = 沿用交易域 |

要点：

- 未知键 → WARNING 并被忽略（不报错）。
- `exclude_st` / `exclude_new_stock` / `exclude_loss` 三个布尔规则**默认关闭**：未声明 = 不过滤、不 preload 对应列、也不告警（不再“假开启”）。显式开启而 ST/上市日期能力缺失时告警一次并跳过相应规则；亏损过滤缺 eps 时告警并尝试 pe_ttm 回退。`exclude_loss` 的 `eps` 缺个别行（非整列）时，引擎在 preload 汇总告警一次（含缺失行数与北交所行数），这些行仍按 pe_ttm<=0 回退；无法判定时保留。
- `index_universe` / `factor_universe` 可用的指数代码取决于后端数据库已有的成分数据。缺 `get_index_members` 方法：preload 裁剪与 `StockFilter` 逐日白名单**各告警一次**，分别回退全市场 / 白名单不生效。方法存在但无成分快照：preload 裁剪**静默**回退全市场（`resolve_index_snapshots` 先查 `[start-45d, end]`，再回退查全历史，仍空则弃用），`StockFilter` 构造时告警一次。详见[后端指南的能力缺失速查](backend_guide.md#10-能力缺失行为速查)。
- `StockFilter` 是策略侧工具：引擎不过滤，策略在 `select()` 中自行调用 `self._filter.filter(bars, date_str)`。

<a id="condition-config"></a>
### conditions 配置

```yaml
conditions:
  stop_loss_pct: 0.06    # 跌 6% 止损 → STOP_LOSS at entry_price × (1 - 0.06)
  take_profit_pct: 0.25  # 涨 25% 止盈 → TAKE_PROFIT at entry_price × (1 + 0.25)
  trailing_pct: 0.08     # 从持仓期最高收盘价回撤 8% → TRAILING_TP at high × (1 - 0.08)
  model_exit:            # 持仓 bar 中 ml_<model> 分数 ≥ threshold → ML_EXIT（次日开盘成交）
    - {model: tb_guard, threshold: 0.6}
```

- `stop_loss_pct`、`take_profit_pct`、`trailing_pct` 必须是 `(0, 1)` 内的数值，否则加载时报错；未知 `conditions` 键直接 `ValueError`。
- `model_exit` 是 `[{model, threshold}]` 列表：`model` 必须在 `models` 节已声明（缺省 `threshold=0.5`，须 ∈ (0,1)）；引用未声明模型在加载时 `ValueError`。
- 所有规则由 `ConditionBuilder` 翻译为条件单 dict，基类默认 `calc_conditions` 已委托 `self._cond.calc()`（见 calc_conditions 接口）；覆盖 `calc_conditions` 时自行 `self._cond.calc()` 取默认条件单再增删改。

### `factor_library`

```yaml
factor_library: factors.yaml   # 相对于策略 YAML 所在目录
```

指向自定义因子库 YAML，覆盖默认 `factors/library.yaml`。详见 [因子库指南](./factor_library.md)。

<a id="conditions"></a>
## 条件单

#### 声明式离场条件（ConditionBuilder）

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
| `ML_EXIT` | 次日开盘价直接成交（open 非法则当日不成交） | — | — |

条件单按**列表顺序**评估，首条触发生效即停止。一般顺序：止损 → 止盈 → 移动止盈。

<a id="buy-conditions"></a>
### 条件买入

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

<a id="custom-conditions"></a>
### 自定义 handler

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
register_condition_handler("MY_CONDITION", my_handler, required_keys={"price"})
```

然后在 `calc_conditions` 中返回含此 type 的条件单：

```python
def calc_conditions(self, symbol, entry_price, bar, holding_days):
    conds = self._cond.calc(symbol, entry_price, bar, holding_days)
    conds.append({"type": "MY_CONDITION", "price": entry_price * 0.9, "custom_param": value})
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

离场注册的可选 required_keys 会在决策点检查缺键；注册是进程级的，应在 on_start 完成。

自定义 handler 返回的 `fill_price` 同样经过价格合法性、涨跌停、成交量 cap、现金检查等全部撮合护栏。

<a id="execution"></a>
## 撮合与执行

**手动买卖**（`buy`/`sell` 名单，trigger = `"MANUAL"`）：

- 执行价 = `execution_price` 指定的次日开盘价或收盘价
- 等权分配：每只约 `执行时点总资产 / max_positions`，再受剩余现金约束（按剩余待买数均分）
- 加权分配：`buy_weights` 存在时每只买入 `min(total_value × weight, cash)`
- `sell_shares` 对指定 symbol 只卖出指定股数（受成交量 cap 截断），其余保留
- 买入只针对新标的（已在持仓中的 symbol 自动跳过）

**目标市值调仓**（`target_value`，trigger = `"TARGET"`）：先卖后买释放现金；target=0 清仓（零碎股一并卖出，当前实现不受整手和成交量 cap 约束）；加仓按加权均价更新成本并锁仓一天。

**通用护栏**（所有买卖路径）：

- **滑点**：买 +n tick、卖 −n tick（1 tick = 0.01 元）。成交记录的 `price` 已含滑点。
  手动单、target 调仓、条件买卖均先校验原价，再校验滑点后价格；滑点后买价达到或
  超过涨停、卖价达到或低于跌停、价格非正时，跳过该订单并告警，现金和持股不变。
  条件单校验使用 `condition_slippage_ticks`（None 时沿用账户滑点）。
- **费用**：佣金（`max(成交额 × commission_rate, min_commission)`）+ 卖出印花税 + 过户费。
- **成交量 cap**：`order_volume_ratio` 配置时单笔股数 ≤ `int(vol(手) × ratio) × 100`。
- **价格非法**：成交价 None / NaN / 非正 → 跳过。条件单成交价非法时当日不成交；是否检查后续条件取决于 handler 返回与撮合分支。条件单在收盘重新生成，无自动顺延保证。
- **现金不足**：跳过该订单并告警，不缩股。
- **T+1 锁定**：买入当日 `Holding.locked = True`，次日解锁。锁定期间条件单自动跳过——不会当天买入当天止损卖出。
- **无当日行情**：标的停牌/缺数据（当日无 bar）→ 该标的当日所有订单跳过并告警（与涨跌停跳过同一告警通道）。卖出单不会顺延到次日重试——pending 每日由 `select()` 重算，平仓意图需策略在次日重新声明。
  若整个加载股票池当日无 bar，仍处理公司行为、以持仓 `last_price` 结算并记录
  `account_daily`；持仓交易日计数照常递增、T+1 解锁，当日 pending 和持仓条件单失效。
  此日不调用 `on_fills/on_tick/select/calc_conditions`，不生成次日信号；恢复有行情后的
  决策日才重新声明订单。整个回测区间都没有行情仍 fail-fast，不生成 completed run。
- **除息日停牌**：现金分红在除息日按持仓照常入账、成本相应扣减；但当日无 bar 时无法按 `pre_close` 做价格除权缩放（`entry_price`/条件单价格保持除权前口径）。复牌后市场价已除权，与持仓口径存在除权差，可能影响裸价收益计算或条件单触发——已知取舍：停牌期除权价不可观测，不引入启发式。
- **红利税预扣**：现金分红按除息日持仓时长预扣——≤30 天 20%、≤365 天 10%、>365 天免征（`btcore/corporate.py`）。该模拟不做卖出时按最终持有期的税额调整，可能早扣/多扣，现金序列与券商账单有差——模拟税制的设计取舍。
- **除权日 trailing 锚点**：移动止盈锚点（持仓期最高收盘价）在除权除息日按除权比例同步缩放（引擎在 `corporate.adjust` 后同步 `ConditionBuilder` 锚点），避免除权价格台阶被误判为回撤而误触发。
- **涨跌停**：
  - 涨停不买：`fill_price >= up_limit` → 买单跳过。
  - 跌停不卖：`fill_price <= down_limit` → 卖单跳过（次日需策略重新声明）。
  - 涨跌停无法判定（`up_limit`/`down_limit` 缺失且无法由 `pre_close` 按板块规则推导）→ 买卖双双跳过。
  - 板块涨跌停幅度：主板 10%，创业板（300/301，2020-08-24 起）与科创板（688）20%，北交所 30%。

<a id="bars"></a>
## bar 与历史查询

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

<a id="python-api"></a>
## Python 调用

```python
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy

strategy = load_strategy("strategies/my_strategy/config.yaml")
provider = DataProvider(backend)
engine = Engine(strategy, provider, db_path="results/run.duckdb", debug=False)
# Engine(strategy, provider, initial_capital=None, db_path=None,
#        max_positions=None, debug=False) —— initial_capital/max_positions 覆盖 YAML

result = engine.run("20240101", "20240630")
# result["account_daily"]   → DataFrame，每日账户快照
# result["trade_log"]       → DataFrame，所有成交
# result["statistics"]      → dict，统计指标（见下）
# result["benchmark_nav"]   → list[float] | None，基准净值序列
# result["benchmark_code"]  → str | None，基准代码
```

<a id="holding"></a>
## Holding

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

## 策略工具签名

| 工具 | 返回与注意事项 |
| --- | --- |
| bars_to_df(bars) | symbol 索引 DataFrame；空输入为空表 |
| eval_factor_specs(df, specs) | (factor_df, score)；百分位加权，空 specs 得分全 1.0 |
| ConditionBuilder(rules).calc(symbol, entry_price, bar, holding_days) | list[dict]，顺序为止损、止盈、trailing、model_exit |
| ConditionBuilder.prune(live_symbols) | 清理已平仓标的锚点 |
| StockFilter(backend, start_date, rules, end_date=None).filter(bars, date_str) | 过滤后的 bars；不是引擎自动入场拦截 |
| build_strategy(cls, config, factor_specs=None, filter_rules=None, factor_library=None, models=None) | 与 YAML loader 共用装配链；conditions 放 config 中；模型路径使用时核对 base_dir |

时间门控、排名阈值、冷却、部分减仓、多因子投票和市场状态机见
[策略模式](strategy_patterns.md)。ML 特征/模型契约由 [ML 指南](ml_guide.md)维护。


查询补充：历史 bars 与基准收益不含钳制后的 end_date 当日，lookback_days 是日历天；
当前截面从钩子 bars 读取。attach_bars 后只可查预载范围；返回切片勿原地修改。
`.backend` 直访不受锚点拦截。首期指数快照和行业历史限制见[后端边界](backend_guide.md)。

买入名单和条件买单合并校验包含 on_tick 的订单；同一 symbol 不得既挂条件单又列入 buy。
条件单只在下一交易日有效；不成交后是否再下单由下一次决策决定。
