---
name: ddup-strategy-craft
description: ddup 策略编写权威规程：五要素填空、L0-L4 阶梯与示例地图（含 multi_model 与 ML 消歧）、Strategy 钩子接口、select 返回协议 7 键与冲突校验、YAML config 键全集、条件单系统与自定义 handler、filter_rules 全集、代码层禁令。创建/修改策略、加条件单/目标仓位/条件买入/自定义 handler、升级策略级别时使用。
---

# ddup 策略编写规程

写策略 = 填五要素 + 选 L 级别 + 抄最近示例改。API 细节见 `docs/strategy_guide.md`（按需切片读）。ML 模型见 `ddup-ml-research`。

## 0. 五要素（写之前逐项填空，填不满=临场发挥）

1. **信号周期**：select() 每日运行；调仓节奏策略自管（时间门控/排名阈值），非调仓日返回空名单
2. **入场两层**：过滤=方向（filter_rules + factor_specs.ascending），触发=下单时点（buy 名单 / buy_conditions）
3. **出场**：至少一条离场路径（conditions / calc_conditions），多路径写清优先级（首条触发即 break）
4. **仓位**：先固定（等权/top_k），逻辑验证后再 buy_weights/target_value
5. **边界**：空名单合法；同向信号不加仓（引擎默认）；反向信号不对锁（要写进 calc_conditions）；涨跌停/量不足=静默跳过并 warn

## 1. L0-L4 阶梯与示例地图（禁跳级，当前级跑正且理解后再升）

| L | 能力 | 示例（strategies/examples/） |
|---|---|---|
| L0 | 过滤→eval_factor_specs 打分→top_k 买卖→ConditionBuilder 条件单 | bare_bones（最小骨架） |
| L1 | on_fills 成交感知（冷却期）、buy_weights 加权、holding_days 自适应条件 | rolling_ranker |
| L2 | target_value 精确仓位、sell_shares 部分减仓、时间门控降频 | target_allocator |
| L3 | buy_conditions 条件买入（LIMIT_BUY/BREAKOUT_BUY/自定义）、自定义离场 handler | condition_hunter |
| L4 | 坍缩因子市场状态门控、多套 factor_specs 独立打分动态加权、交易域/因子域分离 | multi_model |
| 变体 | 自管理调仓：时间门控（非调仓日只卖不买+紧急卖）/ 排名阈值（每持仓独立判断） | self_managed_time / self_managed_rank |

**术语消歧**：multi_model = 多套因子打分投票 + 市场状态机，**无 ML models 节**；ML 模型是另一正交维度（ddup-ml-research）。

## 2. Strategy 接口（btcore/strategy.py）

- `on_start(provider, first_date, end_date=None)`：preload 后调一次。**覆盖必须 super()**（基类接线 StockFilter/ConditionBuilder，否则 filter_bars 报 RuntimeError）；自定义 handler 在此注册
- `select(bars, account_snapshot, provider) -> dict`：abstractmethod，每日调用（含首日前一日预跑播种）；bars=当日截面 dict-of-dicts
- `calc_conditions(symbol, entry_price, bar, holding_days) -> list[dict]`：每持仓每日；基类默认委托 ConditionBuilder
- `on_fills(trades, provider)`：每日决策点最先调（首日空列表）；冷却期记账在这
- `on_tick(bars, snapshot, provider) -> dict|None`：on_fills 后 select 前；**返回只允许 buy_conditions 一个键**，其余 ValueError；基类默认 _cond.prune(持仓)——覆盖时 super() 否则 trailing 状态泄漏
- `get_universe / get_factor_universe`：裁剪交易域/因子计算域；配 index_universe/factor_universe 时 loader 自动生成（指数成分前溯并集）
- 类变量：`REQUIRED_FIELDS` 默认 ["open","high","low","close","vol","adj_factor"]——**命令式访问的扩展列必须声明**（未声明被列裁剪丢弃，决策时点 KeyError；基础 OHLCV/因子列自动覆盖无需声明）；`FACTOR_SPECS` 条目 {factor*, weight=1.0, ascending=False, materialize_only=False}（ml_ 列引用规则见 ddup-ml-research；materialize_only=只物化不参与评分）；`FILTER_RULES`={}

## 3. select() 返回协议（engine 校验，违反即 ValueError）

合法键仅 7 个：`buy` `sell` `buy_conditions` `target_value` `sell_shares` `buy_weights` `sell_reasons`（未知键仅 warning）。

- buy/sell：list，无重复 symbol；buy∩sell 冲突报错
- target_value：dict{symbol: ≥0 有限值}；**与 buy/sell/buy_conditions 全互斥**；0=清仓、未出现不动；trigger="TARGET"，先卖后买
- sell_shares：dict，键必须 ⊆ sell，值正整数
- sell_reasons：dict{symbol: 非空字符串}，键必须 ⊆ sell；按 symbol 覆盖手动卖出的 trigger（缺省 "MANUAL"）——卖出来源归因（如 TREND_BREAK 盘前评估离场），on_fills 冷却期记账与 ML holding 标签都消费 trigger
- buy_weights：dict，键与 buy **精确一致**，单项∈(0,1]、和≤1；None=等权
- buy_conditions：list[dict]，每条必含 symbol/type/price(>0)，value 与 shares 恰填一个；symbol 不得与 buy/sell 重叠；T 日声明 T+1 盘中触发，未触发自动失效
  - **互斥是同日全量校验**：on_tick 返回的 buy_conditions 会与同日 select 的 buy 名单合并校验——若 on_tick 对某 symbol 挂了条件单，select 必须从 buy_list 排除同一 symbol（否则 ValueError）。
    惯用模式（见 condition_hunter）：on_tick 把已挂单 symbol 记入 `self._breakout_symbols`，select 里 `buy_list = [s for s in buy_list if s not in self._breakout_symbols]`，调仓日再重置该集合。
  - **未触发 = 不成交 = 空仓**：条件单 T+1 没碰到触发价就自动失效，该标的要等下个调仓日才重新评估。入场换条件单会系统性漏掉次日高开的强势股——对截面选股型策略（alpha 在选股不在择时）通常是负贡献，改入场方式前先想清楚 alpha 来源
- 空返回 {"buy": [], "sell": []} 合法；空 bars 提前返回它

## 4. YAML config 引擎键（默认值）

`initial_capital`=1000000、`max_positions`=20（超限仅 INFO 不拦截）、`slippage_ticks`=2（非负 int）、`condition_slippage_ticks`=None（沿用前者）、`execution_price`="open"（open/close）、`commission_rate`=0.00015、`min_commission`=5.0、`stamp_tax_rate`=0.0005（仅卖）、`transfer_fee_rate`=0.00001、`benchmark`=None（自动推导：单指数→该指数，否则 000300.SH；空串=无基准）、`order_volume_ratio`=None（单笔≤vol 手×ratio）、`quiet_skips`=False、`ml_log`。自定义键（top_k/rebalance_interval 等）引擎不消费，config.get() 自读。顶层键：strategy(module:Class)/config/factor_specs/filter_rules/conditions/factor_library(自定义因子库路径)/models。

## 5. 条件单系统

内置卖：`STOP_LOSS`/`TAKE_PROFIT`/`TRAILING_TP`（YAML conditions 键：`stop_loss_pct`/`take_profit_pct`/`trailing_pct`，∈(0,1)）；内置买：`LIMIT_BUY`（回踩）/`BREAKOUT_BUY`（突破）。ML_EXIT 见 ddup-ml-research。
- 成交规则：open 越价按 open，否则按触发价；首条触发 break（推荐顺序：止损→止盈→移动止盈→自定义）
- T+1：当日买入最早次日可触发（撮合 exit 先于 entry + 条件单前一日末附着）
- 涨跌停：涨停不买/跌停不卖（顺延）；量 cap、100 股整手、现金不足跳过不缩股
- ConditionBuilder(rules).calc(...) 生成条件单；trailing 锚点每日更新，**on_tick 必须 super() 或手动 prune(持仓键集)** 否则状态泄漏到已平仓标的
- 自定义：`register_condition_handler(type, handler, required_keys=None)`（卖，handler(holding,cond,bar)->(executed,fill,log)）/ `register_buy_condition_handler(type, handler)`（买，handler(order,bar)）。进程级注册表，**必须在 on_start 注册**；required_keys 提供时缺键决策点 fail-fast

## 6. filter_rules 全集（全部可选，软回退：后端缺数据告警一次继续）

`exclude_st`、`exclude_new_stock`（上市 60 日内）、`exclude_boards`（如 ["BJ","688","300","301"]）、`exclude_industries`（[行业名]）、`min_price`（close< 剔）、`exclude_loss`（eps<0 剔，后端无 eps 列时回退 pe_ttm≤0；声明才 preload eps+pe_ttm。tushare 亏损股 pe_ttm 为 NULL 或正数，eps 才是可靠信号——2026-08 修复）、`index_universe`（指数池白名单，只管入场）、`factor_universe`（只决定因子计算域，不过滤交易）。未知键仅 WARNING。

## 7. 代码层禁令

- select 访问未声明 `REQUIRED_FIELDS` 的扩展列 → 决策时点 KeyError
- 自定义 handler 不在 on_start 注册 → "未注册的条件单类型"
- target_value 与 buy/sell/buy_conditions 同日混用；buy∩sell；名单重复 symbol
- on_tick 返回 buy_conditions 以外的键；忘记 super()/prune()
- 每日状态维护（冷却递减/trailing 更新/状态机）塞进调仓分支——select 每日都跑，维护放 on_tick
- 策略里自查 SQLite 判断市场状态 → 用 provider.get_benchmark_trend()/get_historical_bars()（前视保护查询）
- 研究侧用 `compute_factors` 评估坍缩因子 → 必须 `compute_breadth`（全市场口径，ddup-factor-research）
- 策略自行加载 ONNX 逐日推理 → 绕开前视保护与物化体系，严禁（ddup-ml-research）
