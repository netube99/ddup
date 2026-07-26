# 策略系统指南

策略层位于 `btcore` 之上：声明式 YAML + loader + Strategy 子类。
框架仅提供机制——YAML 加载、过滤器、条件单构建器、调仓调度、风控——
买卖与调仓逻辑由 Strategy 子类的 `select()` 方法实现。
因子定义不在本层，策略只按名字引用 `factors/` 因子库，详见 [因子库指南](factor_library.md)。

依赖关系：`strategies → {btcore, factors}`，下层不得导入本层。

---

## 快速开始

```bash
python scripts/run.py strategies/examples/topk_momentum/config.yaml \
    --start 20240603 --end 20240628 --capital 1000000
```

---

## YAML 完整结构

```yaml
name: my_strategy                                    # 策略名称
strategy: my_package.module:MyStrategy               # 必填：module:Class

config:                                              # 策略配置（原样进 strategy.config）
  initial_capital: 1000000                           # 初始资金（元）
  max_positions: 10                                  # 最大持仓数
  # 以下为引擎识别的可选键（全部有默认值）：
  # benchmark: "000300.SH"                           # 基准代码，默认沪深300；""/None = 无基准
  # slippage_ticks: 2                                # 滑点档数，默认 2（每档 0.01 元）
  # commission_rate: 0.00015                         # 佣金率，默认万分之 1.5
  # min_commission: 5.0                              # 最低佣金（元），默认 5 元
  # stamp_tax_rate: 0.0005                           # 印花税率（仅卖出），默认万分之 5
  # transfer_fee_rate: 0.00001                       # 过户费率，默认十万分之 1
  # order_volume_ratio: 0.1                          # 单笔上限 = 当日 vol(手) × ratio × 100 股
  # execution_price: "close"                         # 手动单成交价：open(默认)|close
  # condition_slippage_ticks: 2                      # 条件单独立滑点档数（缺省沿用 slippage_ticks）
  # quiet_skips: true                                # 小资金场景：撮合跳过消息降级为 DEBUG
  top_k: 5                                           # 策略自定义参数

schedule:                                            # 可选：调仓频率
  frequency: weekly                                  # daily(默认)|weekly|biweekly|monthly
  weekday: -1                                        # weekly/biweekly: 每周第 N 个交易日（可负，-1=最后）
  # monthday: 1                                      # monthly: 每月第 N 个交易日（可负）

factor_specs:                                        # 只引用 factors/library.yaml 里的名字
  - factor: mom20
    weight: 1.0                                      # 可选，默认 1.0
    ascending: false                                  # 可选，默认 false（值大优先）

# factor_library: my_lib.yaml                        # 可选：自定义因子库，相对本 YAML 目录

filter_rules:                                        # 可选：StockFilter 规则
  exclude_st: true                                   # 排 ST
  exclude_new_stock: true                            # 排近 60 天上市新股
  exclude_boards: ["BJ"]                             # 排板块（BJ/688/300/301）
  exclude_industries: []                             # 排行业
  min_price: 3.0                                     # 最低股价
  exclude_loss: true                                 # 排亏损（需 pe_ttm 列，列裁剪触发）
  index_universe: ["000300.SH", "000905.SH"]         # 指数成分白名单，多指数取并集

conditions:                                          # 可选：声明式条件单
  stop_loss_pct: 0.08                                # 止损：成本价 × (1 - pct)，值 ∈ (0,1)
  take_profit_pct: 0.25                              # 止盈：成本价 × (1 + pct)
  trailing_pct: 0.10                                 # 移动止盈：持仓最高价 × (1 - pct)

risk_rules:                                          # 可选：组合级风控
  max_drawdown: 0.15                                 # 回撤 ≥ 15% 触发熔断，值 ∈ (0,1)
  cooldown_days: 5                                   # 熔断后 N 日只卖不买，默认 1
  max_position_pct: 0.10                             # 单票买入 ≤ 总资产 10%
  max_industry_pct: 0.30                             # 单行业 ≤ 总资产 30%（需 backend 提供行业数据）
```

---

## Strategy 钩子

继承 `btcore.strategy.Strategy`，实现以下方法。引擎只通过这些接口与策略交互。

### 三个必选钩子

```python
from btcore.strategy import Strategy
from btcore.filters import StockFilter
from btcore.strategy_tools import ConditionBuilder, bars_to_df, eval_factor_specs


class MyStrategy(Strategy):
    def on_start(self, provider, first_date: str, end_date: str | None = None) -> None:
        """回测开始前调用一次。初始化过滤器、条件单构建器、策略状态等。"""
        self._filter = StockFilter(provider.backend, first_date, self.FILTER_RULES,
                                   end_date=end_date)
        self._cond = ConditionBuilder(self.config.get("conditions", {}))

    def select(self, bars, account_snapshot, provider) -> dict:
        """每日调用，基于当日行情返回次日执行的 pending 动作。"""
        ...

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """每日对每个持仓调用，返回该持仓的条件单列表。"""
        return self._cond.calc(symbol, entry_price, bar, holding_days)
```

### 两个可选钩子

**`get_universe(provider, start, end) -> list[str] | None`**

返回股票池以裁剪 preload 数据范围。返回 None 表示全市场。
配置了 `filter_rules.index_universe` 且未覆盖此方法时，
loader 自动生成默认实现（指数成分区间并集）。

**`on_fills(trades, provider)`**

每日 `select` 之前由引擎调用，告知当日实际成交的订单列表
（trigger 含 MANUAL / TARGET / 各条件单类型）。
无成交时为空列表；回测首日前也会以空列表调用一次。
典型用途：止损后冷却期管理、按 trigger 区分退出原因、
精确重置 trailing 锚点。同一份 trades 也可在 select 里通过
`account_snapshot.trades` 读取。

---

## select() 返回协议

`select()` 返回一个 dict，引擎据此生成次日执行的 pending 动作。
**所有校验都在 `_compute_pending` 中完成，格式错误立即 ValueError。**

### 形式一：买卖名单（与 target_value 互斥）

```python
return {
    "buy": ["000001.SZ", "000002.SZ"],    # 买入名单
    "sell": ["600000.SH"],                # 卖出名单
}
```

同日 `buy ∩ sell` 必须为空，否则 `ValueError: 同日买卖冲突`。
买入按等权分配（总资产 × 1/max_positions），卖出为清仓。
新买入受 `max_positions` 硬上限约束，达到上限后跳过后续买入。

可选附加 `buy_weights` 自定义买入金额：

```python
return {
    "buy": ["000001.SZ", "000002.SZ"],
    "sell": [],
    "buy_weights": {"000001.SZ": 0.06, "000002.SZ": 0.04},
}
```

权重 ∈ (0, 1]，键必须 == buy 名单，Σ ≤ 1。
每个买入金额 = `total_value × 权重`。格式错误立即 ValueError：
- `buy_weights 的键必须与 buy 名单一致`（多了或少了）
- `buy_weights[xxx] 必须 ∈ (0,1]`（越界或非正数）
- `buy_weights 权重之和必须 ≤ 1`

可选附加 `sell_shares` 做部分卖出：

```python
return {
    "buy": [],
    "sell": ["600000.SH"],
    "sell_shares": {"600000.SH": 500},   # 卖 500 股，剩余持仓保留
}
```

键必须 ⊆ sell，值为正整数股数。校验规则：
- `sell_shares 的 {symbol} 不在 sell 名单里`（键不在 sell 中）
- `sell_shares[{symbol}] 必须是正整数股数`（非正整数或 bool）

### 形式二：目标仓位（与买卖名单互斥）

```python
return {
    "buy": [],
    "sell": [],
    "target_value": {
        "000001.SZ": 100000.0,    # 目标市值 10 万
        "000002.SZ": 50000.0,     # 目标市值 5 万
    },
}
```

次日撮合把持仓调向目标市值，成交 trigger="TARGET"。
列出的标的按目标调仓（未列出的持仓不动，显式给 0 = 清仓）；
高于目标的部分卖出、低于目标加仓或新买。
加仓按加权均价更新 `entry_price`，新买/加仓的标的 T+1 锁定。
仍受 `max_positions` 上限约束。

两种形式同日混用立即 ValueError：
`target_value 与 buy/sell 名单互斥, 同日只能用一种`

### 形式三：条件买入（可附加在形式一上，与 target_value 互斥）

```python
return {
    "buy": [],
    "sell": [],
    "buy_conditions": [
        {"symbol": "000001.SZ", "type": "LIMIT_BUY", "price": 9.80,
         "value": 50000.0},
    ],
}
```

T 日声明、T+1 盘中触发、**单日有效**（未触发即失效，需每日重新声明）。
每个订单必填 `symbol` / `type` / `price`，在 `value`（金额元）/ `shares`（股数）
中**恰填一个**。格式错误立即 ValueError：
- `buy_conditions[i] 缺必填键: {'symbol'}`（缺键）
- `buy_conditions[i] 必须在 value/shares 中恰填一个`（两个都填或都没填）
- `buy_conditions[i].price 必须是正数`（非法价格）
- `buy_conditions[i] value/shares 必须是正数`（非法规模）

内置 type：
- `LIMIT_BUY` — 限价买单：`open <= price` 按 open 成交；否则 `low <= price` 按 price 成交
- `BREAKOUT_BUY` — 突破买单：`open >= price` 按 open 成交；否则 `high >= price` 按 price 成交

约束与手动买一致：涨停不买、停牌跳过、成交量 cap、现金不足减手数、
max_positions 上限、已持仓标的不重复入场、成交即 T+1 锁定。
撮合顺序在手动单 + 条件卖单之后（吃到卖出释放的现金），
成交 trigger 为 type 名。

冲突校验：
- 与 `sell` 名单重叠 → `ValueError: 同日卖出与条件买入冲突`
- 与 `buy` 名单重叠 → `ValueError: buy 名单与条件买入重复`
- 与 `target_value` 同用 → `ValueError: target_value 与 buy_conditions 互斥, 同日只能用一种`
- 未注册的 type → `ValueError: 未注册的条件买入类型: 'xxx'`

### 冲突校验总表

| 组合 | 结果 |
|---|---|
| buy + sell 交集非空 | `同日买卖冲突` |
| buy/sell + target_value | `target_value 与 buy/sell 名单互斥` |
| sell + buy_conditions 交集非空 | `同日卖出与条件买入冲突` |
| buy + buy_conditions 交集非空 | `buy 名单与条件买入重复` |
| target_value + buy_conditions | `target_value 与 buy_conditions 互斥` |

---

## T+1 锁定与涨跌停约束

买入当日持仓标记为 `locked=True`，次日 `_compute_pending` 中解锁。
锁定期间条件单跳过该持仓（不会当天买入当天止损卖出）。

涨跌停：
- **涨停不买**：买入时 `fill_price >= up_limit` 或涨跌停无法判定时跳过
- **跌停不卖**：卖出时 `fill_price <= down_limit` 或无法判定时跳过
- **价格非法**：None / NaN / 非正的 open 在撮合入口直接跳过
- **NaN close**：不进入结算估值，沿用 `last_price`

涨跌停价无法判定（NaN limit）时，该股买卖均跳过，条件单顺延。

---

## 滑点模型

滑点 = 价格方向偏移，单位是 tick（1 tick = 0.01 元）。公式：

```
fill_price = round(price + direction × ticks × 0.01, 2)
```

- 买入：direction = +1（买贵一点）
- 卖出：direction = -1（卖便宜一点）

默认 `slippage_ticks: 2`（即 ±0.02 元），可通过 config 覆盖。
条件单可选独立滑点参数 `condition_slippage_ticks`（非负整数），
缺省时沿用全局 `slippage_ticks`。手动单与 target_value 不受条件单滑点影响。

---

## 条件单（条件卖出）

由 `ConditionBuilder` 根据 YAML 的 `conditions` 声明生成，
策略在 `calc_conditions` 中委托给它：

```python
def calc_conditions(self, symbol, entry_price, bar, holding_days):
    return self._cond.calc(symbol, entry_price, bar, holding_days)
```

内置三种：
- **STOP_LOSS**：`open <= 止损价` 按 open 成交；否则 `low <= 止损价` 按止损价成交
- **TAKE_PROFIT**：`open >= 止盈价` 按 open 成交；否则 `high >= 止盈价` 按止盈价成交
- **TRAILING_TP**：触发规则与 STOP_LOSS 相同，价格由 ConditionBuilder 动态更新为 `持仓最高价 × (1 - trailing_pct)`

移动止盈的最高价由 ConditionBuilder 内部跟踪。平仓后需要在 `select()` 中调用
`self._cond.prune(current_holdings_keys)` 清理已平仓标的的状态。

### 自定义条件单 handler

通过 `match.conditions` 的注册函数扩展自定义类型（买卖两侧均可）：

```python
from btcore.match.conditions import register_condition_handler, register_buy_condition_handler

def my_exit_handler(holding, cond, bar):
    """返回 (executed: bool, fill_price: float, log_params: dict)"""
    ...

def my_entry_handler(order, bar):
    """返回 (executed: bool, fill_price: float, log_params: dict)"""
    ...

# 注册时机：必须在策略 on_start 中完成（不能是类级别，因为注册是进程级全局状态）
register_condition_handler("MY_EXIT", my_exit_handler)
register_buy_condition_handler("MY_ENTRY", my_entry_handler)
```

注册是进程级全局操作：同一进程中多次 `load_strategy` 或
多个 run 共享注册表。在 `on_start` 中注册即可保证首次使用前就位。
type 名未注册时引擎在 `_compute_pending` 阶段立即抛 `ValueError`，
不会拖到次日撮合才发现。

---

## filter_rules 全部规则

策略在 `on_start` 中创建 `StockFilter`，传入 `self.FILTER_RULES`（由 YAML 注入）：

| 规则 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `exclude_st` | bool | true | 排 ST。需要 backend 提供 `get_st_map`，缺失时告警一次，该规则不生效 |
| `exclude_new_stock` | bool | true | 排近 60 天上市新股。需要 backend 提供 `get_recent_listings`，缺失同软回退 |
| `exclude_boards` | list[str] | [] | 排板块（BJ / 688 / 300 / 301） |
| `exclude_industries` | list[str] | [] | 排行业。需要 backend 提供 `get_stock_industries`，缺失同软回退 |
| `min_price` | float | 0.0 | 最低收盘价。0 = 不限制 |
| `exclude_loss` | bool | true | 排亏损（pe_ttm ≤ 0）。依赖 pe_ttm 列：列裁剪下只有显式声明此规则为 true（即 `exclude_loss: true`）才会 preload 该列。列缺失时告警一次，该规则不生效 |
| `index_universe` | list[str] | [] | 指数成分白名单（入场闸）。需要 backend 提供 `get_index_members`，缺失同软回退。多指数取并集，成分按月频快照取最近一期 ≤ 当日。**持仓被调出指数不强制卖出** |

**软回退 vs fail-fast**：过滤规则的鸭子类型依赖（ST 表、行业、指数成分等）缺失时
告警一次（`logger.warning`），该条规则不生效，策略继续运行。
这些数据不是所有数据源都提供，所以用软回退。
伪列引用（`idx_ret` / `industry` / `log_mktcap`）缺失时则 preload 直接报错——
因为策略明确依赖，缺失说明配置错误。

---

## 组合级风控（risk_rules）

引擎在每日 `select()` 之后强制执行，**卖侧永不干预**。
机制在 `btcore/risk.py`，纯函数，不依赖引擎内部状态。

### 回撤熔断（max_drawdown + cooldown_days）

当日结算后总权益自峰值（含期初资金）回撤 ≥ `max_drawdown` 即触发。
触发次日**强制清仓全部持仓**（卖出单 `trigger="RISK"`），
冷却 N 个交易日内所有买侧动作被丢弃（策略 `select` 照常调用，
但 buy/target_value/buy_conditions 一律被覆盖为空或收缩至零）。
冷却结束后自动恢复；再次破阈值会再次触发。

在 trade_log 中可区分风控强平（trigger="RISK"）与策略正常卖出（trigger="MANUAL"）。

### 单票上限（max_position_pct）

作用于买侧：`target_value` / `buy_conditions.value` / `buy_weights`
中超限部分收缩到 `总资产 × pct`。
`shares` 口径的条件买单不收缩——成交价在撮合前未知，属于策略显式数量单。

### 行业上限（max_industry_pct）

"入场闸"不是"持续配平器"：只在买入时点把关（持仓 + 新买单声明的行业暴露）。
名单买单（等权/加权）超限直接丢弃（金额不可收缩），
target_value / buy_conditions.value 收缩到行业剩余额度。
卖出释放的行业额度**当日不回补**。持仓因上涨自然超限不强制减仓。

需要 backend 提供 `get_stock_industries` 方法。
引擎构造时不检查（允许动态注入），但该能力缺失时 `apply_risk_rules`
内部直接 `ValueError`。

---

## schedule 调仓调度

YAML 的 `schedule` 键让 loader 用 `wrap_strategy` 包装策略实例：
非调仓日 `select` 返回空买卖名单（持仓不动），
但 `calc_conditions` 不受影响（条件单仍每日生成并触发）。

```yaml
schedule:
  frequency: weekly     # daily(默认)|weekly|biweekly|monthly
  weekday: -1           # weekly/biweekly: 每周最后一个交易日
```

分组语义：weekly 按 ISO (isoyear, isoweek)、monthly 按 (year, month)；
N 为 1 起的组内第 N 个交易日，负数从组尾倒数。
N 超出组内天数时该组不调仓。
未知键/值在加载期 `ValueError` 快速失败。

---

## Snapshot

`select()` 第二个参数 `account_snapshot` 是 `Snapshot` 具名元组：

- `cash`：当日结算后现金
- `holdings`：当前持仓的**深拷贝只读副本**（改动不影响引擎状态）
- `trades`：当日成交列表（同 `on_fills` 收到的列表）
- `total_value`：当日结算后总权益（现金 + 持仓 × last_price）

---

## 裸价与后复权：不可混用

撮合、成本、估值使用**裸价**（`open` / `close`）；
因子计算、排名使用**后复权**（`*_hfq`，由引擎从 `adj_factor` 精确派生）。

在策略 `select()` 里，`bars` dict 中的 bar 同时包含两套价格。
用裸价做排序会因除权除息产生虚假信号，因子排序须使用后复权列。

---

## REQUIRED_FIELDS 与列裁剪

引擎 preload 时按策略声明静态推导所需列，传给 backend 的
`query_bars(..., columns=...)`：

- 必需 10 列（`open` `high` `low` `close` `vol` `amount` `adj_factor` `pre_close` `up_limit` `down_limit`）
- `REQUIRED_FIELDS`（策略声明需要的非基础列）
- 因子闭包 `FACTOR_NODES` 引用的基础列
- `FILTER_RULES` 显式开启项的依赖（`exclude_loss: true` → `pe_ttm`）

引擎派生列（`*_hfq` / `pct_chg`）和伪列（`idx_ret` / `log_mktcap` / `industry`）
不向 backend 请求。物化因子列也不请求。

**若在 `select()` 里命令式访问某个列（如 `bar["turnover_rate"]`），
必须将其声明到 `REQUIRED_FIELDS`**。否则列裁剪下 backend 不返回该列，
bar dict 中缺失——基础列缺失则 KeyError，extra_fields 列则 get 返回 None。

```python
class MyStrategy(Strategy):
    REQUIRED_FIELDS = [
        "open", "high", "low", "close",
        "vol", "amount", "adj_factor",
        "turnover_rate",   # 我的 select 要用换手率
    ]
```

---

## Engine 程序式 API

除了 CLI，也可以直接在 Python 脚本中使用 Engine：

```python
from adapters.tushare import TushareBackend
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy

strategy = load_strategy("strategies/examples/topk_momentum/config.yaml")
provider = DataProvider(TushareBackend("/path/to/market.db"))
engine = Engine(
    strategy, provider,
    initial_capital=2_000_000,   # 可选，覆盖 YAML config
    db_path="result.db",          # 可选，回测结果库路径（默认 :memory:）
    max_positions=15,             # 可选，覆盖 YAML config
)
result = engine.run("20240101", "20240630")
print(result["statistics"])
```

`Engine.__init__` 参数：
- `strategy`：Strategy 实例
- `provider`：DataProvider 实例，包装了数据后端
- `initial_capital`：可选，覆盖 YAML config 中的 `initial_capital`
- `db_path`：可选，结果库路径，默认 `:memory:`（不落盘）
- `max_positions`：可选，覆盖 YAML config 中的 `max_positions`

`run(start, end)` 返回 dict：
- `"account_daily"`：每日账户快照 DataFrame
- `"trade_log"`：完整成交记录 DataFrame
- `"statistics"`：绩效指标 dict（年化收益、夏普、最大回撤等），其中
  `trading_friction`（交易磨损：双边磨损率、年化拖累、成本占盈利比、无摩擦对照收益）
  与 `management_complexity`（持仓管理复杂度：单日最大成交笔数、有成交天数占比、
  单笔买入金额门槛、单票平均市值）是面向散户手动跟单场景的两组指标；
  `sell_source` 按卖出 trigger（MANUAL/TARGET/RISK/条件单类型）分组统计
  round-trip 盈亏、胜率与平均持仓天数，用于回答"哪类卖出在赚钱/亏钱"

---

## 结果库 schema

回测结果库是 SQLite 文件，四张表：

### runs
| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER PK AUTOINCREMENT | 每次 run 自增 |
| `created_at` | TEXT | 创建时间戳 |
| `strategy` | TEXT | 策略类名 |
| `start_date` / `end_date` | TEXT | 回测区间 |
| `initial_capital` | REAL | 初始资金 |
| `config_json` | TEXT | 策略完整配置 JSON |
| `status` | TEXT | running / completed / failed |
| `stats_json` | TEXT | statistics 完整 JSON（多 run 对比用；老库 ALTER 迁移，历史 run 为 NULL） |

### account_daily
| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER | 关联 runs |
| `date` | TEXT | 交易日 |
| `cash` | REAL | 现金 |
| `total_value` | REAL | 总权益 |
| `daily_pnl` | REAL | 当日盈亏 |
| `cumulative_pnl` | REAL | 累计盈亏 |
| `initial_capital` | REAL | 初始资金（冗余） |
| `n_holdings` | INTEGER | 持仓数 |

### trade_log
| 列 | 类型 | 说明 |
|---|---|---|
| `run_id` | INTEGER | 关联 runs |
| `date` | TEXT | 成交日 |
| `symbol` | TEXT | 证券代码 |
| `side` | TEXT | BUY / SELL |
| `trigger` | TEXT | MANUAL / TARGET / STOP_LOSS / TAKE_PROFIT / TRAILING_TP / LIMIT_BUY / BREAKOUT_BUY / RISK / CORPORATE |
| `price` | REAL | 成交价（已含滑点） |
| `shares` | INTEGER | 成交股数 |
| `turnover` | REAL | 成交额 |
| `commission` / `stamp_tax` / `transfer_fee` | REAL | 费用明细 |
| `slippage_amount` | REAL | 滑点金额 |
| `net_amount` | REAL | 净现金流 |
| `reason` | TEXT | 备注（现金分红等） |

### holdings（瞬态快照，每 run 清空）
| 列 | 类型 | 说明 |
|---|---|---|
| `symbol` | TEXT PK | 证券代码 |
| `entry_date` | TEXT | 建仓日期 |
| `entry_price` | REAL | 建仓均价 |
| `shares` | INTEGER | 持仓股数 |
| `cost` | REAL | 持仓成本 |
| `conditions_json` | TEXT | 当前条件单 JSON |
| `last_price` | REAL | 最新收盘价 |
| `holding_days` | INTEGER | 持仓天数 |

**多 run 累积**：同一 db_path 多次 run，历史 run 保留（按 run_id 隔离），
新的 run 挂在新 run_id 下。holdings 每次 run 开始清空。
run 中抛异常时 status 改写为 `failed`，不留 `running` 假象。

查询归因分析：
```python
from research.attribution import brinson_attribute

# 签名：brinson_attribute(db_path, provider_db, start, end,
#                         index_code="000300.SH", run_id=None)
# provider_db 是行情库路径（提供行业映射/行业指数/基准权重）

# 默认取最新 run
result = brinson_attribute("result.db", "/path/to/market.db",
                           "20240101", "20240630")

# 指定 run_id
result = brinson_attribute("result.db", "/path/to/market.db",
                           "20240101", "20240630", run_id=3)
```

---

## HTML 报告与多 run 对比

`research/report.py` 提供单文件 HTML 报告（内联 SVG 图表，零第三方依赖，离线可读）：

```bash
# 回测时直接出报告
python scripts/run.py <策略.yaml> --start 20240101 --end 20240630 \
    --out result.db --report report.html

# 从结果库离线生成单 run 报告（--run-id 缺省取最新）
python scripts/report.py result.db [--run-id 3] --out report.html

# 多 run 对比：终端打印关键指标表，--html 同时产出对比报告
# （指标对比表 + 各 run 归一化净值叠加曲线）
python scripts/compare.py result.db [--runs 1,2,3] [--html compare.html]
```

单 run 报告包含：核心指标、净值/回撤曲线、月度收益、交易磨损、
持仓管理复杂度、往返交易汇总、个股盈亏贡献 Top10、成交明细。
老 run（stats_json 为 NULL）在生成报告/对比时现场重算统计指标。

程序式 API：
```python
from research.report import generate_report, generate_compare_report

generate_report(result, "report.html")                      # engine.run() 返回值
generate_compare_report("result.db", "compare.html")        # 全部 run 对比
```

---

## CLI 用法

```bash
python scripts/run.py <策略.yaml> \
    --start YYYYMMDD \         # 必填：回测开始日期
    --end YYYYMMDD \           # 必填：回测结束日期
    [--capital 初始资金] \      # 覆盖 YAML config
    [--out 结果库路径] \        # 默认 :memory: 不落盘
    [--report 报告.html] \      # 缺省生成到 <策略目录>/reports/<yaml名>_<起>_<止>.html
    [--no-report]              # 不生成报告
```

报告默认与策略绑定，生成到策略目录下的 `reports/` 文件夹；该目录已加入
`.gitignore`（`strategies/**/reports/`），回测结果属隐私信息不入 git。

行情数据库路径由 `adapters/tushare.py` 的 `_DEFAULT_DB_PATH` 决定，不存在运行时切换数据源的参数。

---

## 注意事项与常见错误

1. **裸价做因子**：在 `select()` 里用 `close` 而不是 `close_hfq` 做排名，
   除权除息日会产生虚假信号。因子全部用后复权是硬规则。

2. **REQUIRED_FIELDS 没声明**：`select()` 里访问了某个列但没声明到
   `REQUIRED_FIELDS`，列裁剪下该列不会被 preload。`bar.get("turnover_rate")`
   返回 None，静默错误——不会报错，但值永远为空。

3. **buy 与 sell 交集非空**：同日买卖同一标的立即 `ValueError`。
   想先卖后买用 target_value。

4. **target_value 混用 buy/sell**：立即 `ValueError`。两种形式只选一种。

5. **buy_conditions 与 target_value 混用**：立即 `ValueError`。条件买入只能
   附加在买卖名单形式上（buy/sell 都可以为空）。

6. **order_volume_ratio 单位**：vol 单位是手（1 手 = 100 股），
   上限 = `int(vol × ratio) × 100` 股。例如 ratio=0.1、当日 vol=50000 手，
   则上限 = `int(50000 × 0.1) × 100 = 500000` 股。
   这个 cap 适用于所有买卖（手动单、target_value、条件单），
   卖出超限变部分卖出、买入超限部分跳过。

7. **condition_slippage_ticks**：只影响条件单的滑点，手动单和 target_value
   不受此键影响。设为 0 即条件单无滑点。

8. **execution_price = "close"**：T 日信号在 T+1 日收盘价成交，仍是次日撮合
   无前视。条件单有自己的盘中触发模型，不受此键影响。

9. **自定义 condition handler 注册时机**：必须在 `on_start` 中注册。
   如果多个策略在同进程中使用同名 type 但不同 handler，
   后加载的会覆盖先加载的——这是进程级全局注册表的固有特性。

---

## 示例策略（`strategies/examples/`）

每个策略独立一个子文件夹，包含 `strategy.py` + `config.yaml`：

以下 5 个示例涵盖引擎全部能力：

### `topk_momentum` — 基础买卖名单 + 条件单 + 成分白名单

经典多因子打分轮动：每日对全截面按 FACTOR_SPECS 合成得分，
持有得分最高的 top_k 只，卖出不在 top_k 中的持仓。
配合 `stop_loss` + `trailing` 条件单。另有 `topk_momentum_csi` 变体
加上 `index_universe` 指数成分白名单。

### `simple_rotation` — 最小化轮动

最简入门示例：固定持仓数，按因子得分轮换。适合作为新策略的起点。

### `target_allocator` — target_value + risk_rules

演示 `select()` 返回 `target_value` 做目标仓位调仓：
按得分比例分配目标市值，不在 top_k 中的持仓显式给 0 清仓。
配合 `max_drawdown` 回撤熔断 + `max_position_pct` 单票上限，
展示风控规则如何与目标仓位形式配合。

### `condition_hunter` — buy_conditions 条件买入

在常规轮动（buy/sell 名单）之外，对排名紧随其后的备选标的
挂 `LIMIT_BUY` 限价买单，捕捉日内回踩机会。
演示 buy_conditions 与 buy/sell 名单的配合，
以及条件买入的约束（涨停不买、max_positions 上限等）自动生效。

### `state_machine` — 多模型状态机

最完整的示例：3 套子模型（动量/反转/质量）、按市场广度切换权重、
自定义因子库、6 种条件单类型。展示引擎能力的上限。
