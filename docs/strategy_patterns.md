# 策略设计与示例模式

先选择满足需求的最小机制，再查[策略接口](strategy_guide.md)。L0–L4 是示例复杂度地图，
不是必须逐级开发的门禁。multi_model 指多套因子打分与状态机，不是 ML models 模型；
ML 与这些模式正交，见[模型指南](ml_guide.md)。

<a id="spec"></a>
## 五要素规格

| 要素 | 必须说清楚 | 对应机制 |
| --- | --- | --- |
| 信号周期 | 哪些决策日买/卖；非调仓日做什么 | select 内时间门控或排名阈值；on_tick 维护状态 |
| 入场 | 候选过滤、排序方向、价格触发分别是什么 | filter_rules / factor_specs / buy 或 buy_conditions |
| 出场 | sell/target=0/条件离场路径及优先级 | sell_reasons、calc_conditions；首条条件触发后停止 |
| 仓位 | 等权/目标市值/权重，现金缓冲和持仓数 | max_positions 只提示，策略需自行控制 |
| 边界 | 重复信号、缺数据、拒单、无信号、旧持仓如何处理 | 空名单合法；名单买入不加仓，target 可加仓并整仓锁定 |

空 calc_conditions 可以是完整轮动策略：退出已由 sell 或 target_value 表达。
只在规格要求退出而所有路径均缺失时判定规格不完整。
过滤可用于选择做多或空仓；当前引擎没有做空/双向对锁交易系统。

<a id="examples"></a>
## 示例选择

| 示例 | 展示能力 | 源码 |
| --- | --- | --- |
| L0 bare_bones | 过滤、评分、top_k、默认条件单 | [strategy.py](../strategies/examples/bare_bones/strategy.py) |
| L1 rolling_ranker | 成交感知、冷却、buy_weights、持有期条件 | [strategy.py](../strategies/examples/rolling_ranker/strategy.py) |
| L2 target_allocator | 目标市值、时间门控、近边缘减半 | [strategy.py](../strategies/examples/target_allocator/strategy.py) |
| L3 condition_hunter | 条件买入、自定义买卖 handler | [strategy.py](../strategies/examples/condition_hunter/strategy.py) |
| L4 multi_model | 因子投票、市场状态、交易/因子域分离 | [strategy.py](../strategies/examples/multi_model/strategy.py) |
| self_managed_time | 买定时、卖随时 | [strategy.py](../strategies/examples/self_managed_time/strategy.py) |
| self_managed_rank | 排名阈值、逐仓持有期、历史查询 | [strategy.py](../strategies/examples/self_managed_rank/strategy.py) |

示例参数只演示接口，不代表有效阈值或可直接上线的收益基线。

<a id="schedule"></a>
## 自管理调仓

`select()` 每日运行，调仓节奏完全由策略代码自行控制：在 `select()` 中判断今天是否调仓，非调仓日返回空名单即可。引擎不提供声明式调仓调度，也不拦截 `select()` 的输出。

**时间门控模式**（固定间隔全量轮动，非调仓日可紧急卖出）：

```python
def on_start(self, provider, first_date, end_date=None):
    super().on_start(provider, first_date, end_date)
    self._rebalance_interval = int(self.config.get("rebalance_interval", 22))
    # 交易日计数器：初值 = interval 保证首日（prev_day 预计算）即调仓
    self._days_since_rebalance: int = self._rebalance_interval

def select(self, bars, account_snapshot, provider):
    if not bars:
        return {"buy": [], "sell": []}
    current = set(account_snapshot.holdings.keys())

    # 每个决策日 +1，达到 interval 才是调仓日——禁止 int(YYYYMMDD) 日期差：
    # int("20240201") - int("20240122") = 79，跨月/周末会强插调仓
    self._days_since_rebalance += 1
    if self._days_since_rebalance < self._rebalance_interval:
        urgent = [s for s in current if self._needs_urgent_exit(s, bars.get(s, {}))]
        return {"buy": [], "sell": urgent}

    # 调仓日：完整轮动
    self._days_since_rebalance = 0
    # ... 正常排名、选股、卖出逻辑
```

**先明确计数口径**：调仓间隔、`min_hold_days` 最短持有期、冷却期使用交易日/决策日计数，
不得用 `int(trade_date)` 十进制差（日期不是数字，`20240131 + 1 = 20240132` 不会滚动到 2 月）：

- 调仓间隔：上面的 `_days_since_rebalance` 计数器（每个 `select` 决策日 +1）。
- 最短持有期：直接读引擎持仓快照的 `holding.holding_days`（交易日口径，成交当日为 1，
  见 self_managed_rank），或自建 per-symbol 计数器（买入日置 0，
  每个 `select` 日 +1）。
- 冷却期：per-symbol 剩余交易日计数器——`on_fills` 成交时置 N，`on_tick` 每个决策日
  -1，跌穿 0 解除（见 rolling_ranker）。

**排名阈值模式**（每只持仓独立跟踪，排名跌出阈值才卖出，换手由排名变化自然驱动）：见 `self_managed_rank` 示例。

| 对比维度 | 时间门控 | 排名阈值 |
|----------|----------|----------|
| 买侧触发 | 固定间隔 | 有空位即补 |
| 卖侧触发 | 非调仓日紧急卖 / 调仓日全量轮动 | 排名掉出阈值 + 持有够久 |
| 换手频率 | 由 `rebalance_interval` 决定 | 由排名变化速度自然决定 |

自管理让策略可以实现「卖随时、买定时」「调仓日只换 1-2 只」「广度差时跳过本次调仓」等非对称控制。注意：`bare_bones` / `rolling_ranker` 未做自管理，`select()` 每日全量轮动每天重新选股，但仍是 T+1 日频执行——精细化换手必须在代码中显式实现。示例：`self_managed_time`、`self_managed_rank`、`target_allocator`、`multi_model`。

---

<a id="composition"></a>
## 组合机制时的关键选择

- **权重**：buy_weights 是总资产分数，不是新买名单内相对比例。用整个目标组合归一化，
  再取新买标的子集；否则一只新标的可能索要大部分资产。target_value 则直接表达目标市值。
- **冷却**：用 on_fills 的真实 SELL 与 trigger 启动，on_tick 递减；不要把未成交卖单当退出。
- **状态更新**：放 on_tick，保留 super/prune；放在 select 调仓 early-return 后会漏非调仓日。
- **条件离场**：可在默认条件列表上按 holding_days 或波动修改价格，但写明优先顺序；
  动态条件必须每日重新声明，拒单不会由引擎永久保存。
- **部分减仓**：sell_shares 是明确股数，键必须属于 sell；target_value 是市值目标，
  0 清仓、未出现保持。不要在同一天混用两套订单模式。
- **条件买入**：主仓名单与备选条件单的 symbol 分离；on_tick 订单也参与冲突检查。
  未触发就是没有成交，分析时检查漏掉哪些信号，而不是假定条件买入总能优化价格。

<a id="regime"></a>
## 市场状态与多套评分

1. 用 mean 构建全市场广度，或 group_mean 构建行业值；以 materialize_only 物化。
2. 在 on_tick 推进状态机，可用连续决策日确认避免频繁切换。
3. 在 select 用多套 factor_specs 分别 eval_factor_specs，再按当前状态组合分数/仓位。
4. 所有子组合引用的因子仍须进入 loader 的闭包；扩展原始列声明 REQUIRED_FIELDS。

```python
scores = {}
for name, specs in self._models.items():
    _, scores[name] = eval_factor_specs(df, specs)
final = sum(scores[n].fillna(0) * self._model_weights[n] for n in scores)
```

这里 fillna(0) 是示例的缺失值决策，不是引擎通用要求。市场分类、行业分化阈值、
方向确认天数和权重必须在固定数据窗口验证，不能套用某次研究的“已证实”参数。

factor_universe 让截面计算域宽于交易域；全市场坍缩不依赖候选池大小。
扩大计算域会改变截面排名的含义，不自动意味着信号质量提高。

加载池整日无 bar 时不调用策略钩子，因此上述计数器是“有行情的决策日”计数；
引擎 holding_days 仍按交易日递增。要求严格交易日调仓时用日历索引差，不要假定两者恒等。
