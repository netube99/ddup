# ARCHITECTURE.md — ddup 架构文档

A 股日频量化策略回测引擎。Python 3.12+，uv + hatchling，核心依赖仅
pandas / numpy / numexpr / pyyaml（onnxruntime / xgboost 仅 ML 训练推理路径惰性加载）。

本文档定位：模块边界、目录结构、关键函数位置（file:line）、数据流、约定。
设计哲学与教程见 `docs/`；开发规则见 `AGENTS.md`。

---

## 1. 目录结构与分层

```
btcore/        全部机制/基础设施（引擎、ABC、因子机制、撮合、ML 子系统）—— 不要随意修改
  factors/     因子表达式机制：算子表白名单、物化规划、CSE、因子库加载
  match/       撮合：core（共享结算原语）/ conditions（条件单）/ manual（普通买卖）
  ml/          ML 子系统：spec / dataset / trainer / labels / runtime / conditions / metrics / export
adapters/      用户数据后端实现（可编辑）—— tushare.py 是对 GenericSQLBackend 的填表
factors/       用户因子定义 library.yaml（可编辑，88 个因子，纯 YAML 数据）
strategies/    用户策略（可编辑）：examples/ 教学参考、selected/ 精选、exploring/ 实验、archive/
research/      研究工具库（纯 importable 模块，无 CLI）：因子评估、合成、归因、HTML 报告
scripts/       可执行 CLI 入口（回测、报告、评估、训练、扫描、回放、校验）
tests/         pytest 套件（407 测试）+ fixtures/*.parquet + test_invariants/（INV1-INV8）
docs/          设计文档（index.md 是导航入口）
results/       回测结果库（*.db，SQLite，多 run 累积）
```

### 依赖方向（`scripts/check_anticorrupt.py:205` 强制）

```
types.py / constants.py  零依赖，被所有人依赖
match/*                  子模块互不 import，仅可依赖 core.py
stats.py                 纯函数：禁 sqlite3，不依赖 provider/engine
btcore/factors/*         不依赖 engine/match/database/provider
engine.py                不被 btcore 内部模块 import（仅用户代码调用）
btcore/                  不 import strategies/ 顶层 factors/ adapters/（单向）
顶层 factors/            仅依赖 btcore.factors
全局                     无循环 import
```

---

## 2. 核心机制层 btcore/

### 2.1 引擎 engine.py（657 行）—— 主循环

| 符号 | 位置 | 职责 |
|---|---|---|
| `required_bar_columns` | engine.py:19 | preload 列裁剪：契约列 ∪ REQUIRED_FIELDS ∪ filter 列 ∪ fplan main_columns |
| `class Engine` | engine.py:37 | 构造读 strategy.config：initial_capital/max_positions/滑点/benchmark/execution_price |
| `run(start, end)` | engine.py:113 | preload → 因子/ML 物化 → 逐日 step → 统计落库 |
| `_build_factor_plan` | engine.py:250 | FACTOR_SPECS + FACTOR_NODES → 因子供给计划 |
| `_preload_breadth` | engine.py:265 | 广度面板：全市场 × 短窗口 × 窄列（坍缩因子专用） |
| `step(today, day_bars, conn)` | engine.py:291 | 单日：公司行为 → 撮合 → 结算 → 次日决策 |
| `_settle` | engine.py:365 | 估值 + 写 account_daily/holdings/trade_log |
| `_compute_pending` | engine.py:404 | 决策计算：on_fills → on_tick → select → 校验 → calc_conditions |
| `_inject_holding_model_scores` | engine.py:535 | holding scope 模型逐持仓打分，注入 bar dict |
| `_write_ml_predictions` | engine.py:552 | ml_predictions 落盘 |
| `_bars_to_dict` | engine.py:651 | 截面 DataFrame → {symbol: bar dict} |

### 2.2 数据接入：backend.py / provider.py / generic_sql.py

- `DataBackend`（backend.py:53）ABC，三个抽象方法：
  - `query_bars(symbols, start, end, columns)`（backend.py:58）→ MultiIndex(trade_date, symbol)
    面板；契约列 open/high/low/close/vol/adj_factor/pre_close/up_limit/down_limit（裸价）
  - `get_calendar(start, end)`（backend.py:102）
  - `get_dividends_on_date(date)`（backend.py:108）→ {symbol: {stk_div, cash_div}}
- 可选能力鸭子类型（backend.py:120，getattr 探测，缺则软回退）：
  get_benchmark_bars / get_st_map / get_stock_industries / get_recent_listings / get_index_members
- `DataProvider`（provider.py:21）前视防护门面：
  - `get_engine_bars`（provider.py:37）含当日，仅引擎撮合/preload 用
  - `get_historical_bars`（provider.py:58）不含当日，钳制 `min(end, _as_of_date)` 再截到前一交易日
  - `_as_of_date` 由 Engine._compute_pending（engine.py:405）每日设置 —— 前视钳制锚点
- `GenericSQLBackend`（generic_sql.py:117）填表法后端：
  用户声明「表名.字段名」表单 dict，`_compile_form`（generic_sql.py:485）校验编译，
  `_check_schema`（generic_sql.py:371）初始化期落库校验所有表/列引用；
  `query_bars`（generic_sql.py:141）多表 outer join 拼 (交易日,代码) 网格面板，缺行即 NaN。
  示例：`adapters/tushare.py:230 TushareBackend`。

### 2.3 策略层：strategy.py / strategy_loader.py / strategy_tools.py / filters.py

- `Strategy`（strategy.py:8）ABC，只有声明式属性 + 生命周期钩子，无行为开关：
  - 类属性：REQUIRED_FIELDS(:25) / FACTOR_SPECS(:31) / FACTOR_NODES(:32) / MODEL_SPECS(:33) /
    FILTER_RULES(:40) / CONDITION_FACTORS(:41)
  - 钩子：`get_universe`(:58) / `get_factor_universe`(:68) / `on_start`(:81, abstract) /
    `on_fills`(:86) / `on_tick`(:96, 可返回 buy_conditions) /
    `select(bars, snapshot, provider) -> dict`(:121, abstract) /
    `calc_conditions(symbol, entry_price, bar, holding_days) -> list[dict]`(:125, abstract)
- `strategy_loader.build_strategy`（strategy_loader.py:40）YAML/dict → Strategy 管线：
  因子库加载 → `parse_models` → **materialize_only 合并**（:101-110，模型 features 并入因子
  闭包统一物化但不参与评分，raw_features 并入 REQUIRED_FIELDS）→ `resolve_closure` 求
  FACTOR_NODES → 校验 → 实例化挂接 → `ml_conditions.register()`
- `load_strategy(path)`（strategy_loader.py:147）YAML 入口；`_validate_conditions`（:313）；
  `_check_factor_conflicts`（:341，scoring/materialize_only/条件单因子交叉 WARNING）
- `strategy_tools`：策略编写工具 —— `bars_to_df`(:15)、`eval_factor_specs`(:20，截面
  percentile 加权合成 score∈[0,1])、`ConditionBuilder`(:66，YAML conditions → 条件单 dict)
- `filters.StockFilter`（filters.py:25）：ST/新股/板块/行业/亏损/指数白名单过滤，
  一次性 preload，能力缺失告警一次软回退

### 2.4 因子机制 btcore/factors/

| 模块 | 关键符号 | 职责 |
|---|---|---|
| ops.py | `_OPS`（ops.py:204，**固定 dict 非注册表**）、`eval_op_expr`（:367）、`validate_op_expr`（:277）、`has_op_expr`→`has_op_call`（:272）、`infer_window`（:318）、`collapse_kind`（:351） | 算子白名单：ts 族（delay/delta/roc/ma/ema/std/sum/max/min/corr/beta/resid_std）、截面保形（rank/zscore/winsorize/group_rank/neutralize/abs/log）、坍缩（mean/group_mean）；AST 白名单校验 |
| expr.py | `evaluate_expr`（expr.py:46）、`validate_expr`（:22） | 无算子纯表达式 → pandas.eval/numexpr 截面求值，禁函数调用/属性访问 |
| plan.py | `REQUIRED_BAR_COLUMNS`（plan.py:34）、`build_factor_plan`（:157）、`materialize`（:273，两路供给：广度面板物化→投影→主面板物化）、`validate_materialization`（:322）、`ensure_pseudo_columns`（:118，industry/log_mktcap/idx_ret）、`derive_fields`（:83，hfq/pct_chg 派生） | 物化规划：拓扑序、warmup 窗口推导（_to_calendar_days :153 = rows×1.5+10）、广度/主面板分列 |
| cse.py | `rewrite`（cse.py:24） | 公共子表达式消除：相同 AST 去重 + 高频 Call 子树提取为 `__cse_N` 临时节点 |
| library.py | `load_library`（library.py:36）、`resolve_closure`（:138）、`compute_breadth`（:279，坍缩因子流式计算，chunk_days=60） | library.yaml 加载（{name:{expr,where?,description?}}），where 为求值后掩码（False→NaN），DFS 循环检测 |

### 2.5 撮合 btcore/match/

- **core.py**（共享原语）：`is_valid_price`(:4)、`exec_price`(:9)、`cap_by_volume`(:34，
  成交量约束）、`check_tradable`(:63，涨跌停跳过 → LIMIT_UP/LIMIT_DOWN/LIMIT_UNKNOWN)、
  `_execute_trade`(:79，滑点+费用+现金台账）、`execute_sell`(:123)/`execute_buy`(:133)
- **conditions.py**（条件单）：`register_condition_handler`(:23) / `register_buy_condition_handler`(:28)
  进程级注册表；`exit_conditions`(:57) / `entry_conditions`(:166，条件买单最后执行以吃当日
  释放现金）；内置 STOP_LOSS(:120)/TAKE_PROFIT(:138)/TRAILING_TP/LIMIT_BUY(:241)/BREAKOUT_BUY(:256)。
  handler 协议：sell `(holding, cond, bar)` / buy `(order, bar)` → `(executed, fill_price, log_params)`
- **manual.py**（普通单）：`manual_sell`(:22)、`manual_buy`(:81)、
  `rebalance_to_targets`(:166，target_value 调仓，先卖后买）
- 层规：conditions.py 与 manual.py 互不 import，仅依赖 core.py

### 2.6 ML 子系统 btcore/ml/

- **spec.py**：`ModelSpec`（spec.py:43）；`column` 属性 → `ml_<name>`（:74）；
  scope = holding iff state_features 非空（:79）；`feature_order = features+raw+state`（:84，
  训练/推理共享向量序）；`from_dict`（:88）meta v2 契约（META_VERSION=2 :33，
  meta['version']==2 否则 fail-fast）；`parse_models`（:207）
- **dataset.py**：`build_panel`（dataset.py:22）—— **训练与引擎同一物化函数链**
  （build_factor_plan → query_bars → derive_fields → materialize → validate），无第二管线
- **runtime.py**：`materialize_predictions`（runtime.py:88）panel scope 批量 ONNX 推理 →
  写 `ml_<name>` 列（引擎在因子物化后、factor_universe 裁切前调用，engine.py:152-156）；
  `holding_score`（:147）holding scope 决策时点逐持仓打分注入 bar dict；
  `compute_state_features`（:128，hold_days/ret_from_entry 训练推理同源）
- **conditions.py**：`ML_EXIT` handler（conditions.py:18，次日 open 成交，
  复用条件单全部护栏：跌停递延/量 cap/T+1 跳过）
- **trainer.py / labels.py / metrics.py / export.py**：`train_panel`（trainer.py:64，XGBoost
  回归 + 时间切分 embargo :29）/ `train_guard`（:132，holding 二分类）；
  `xs_forward_return`（labels.py:26）；`export_model`（export.py:21，ONNX + meta v2 +
  sklearn/ONNX 预测一致性自校验 :83）

### 2.7 支撑模块

| 模块 | 关键符号 | 职责 |
|---|---|---|
| types.py | `Holding`(:4, locked 默认 True=T+1) / `Account`(:17) / `Trade`(:32) / `Snapshot`(:50) / `bar_get`(:58) | 数据类，零依赖 |
| constants.py | 费率常量、TICK_SIZE=0.01、CAL_DAYS_ANNUAL=244、`PLATE_LIMIT_RULES`(:9，BJ 30%/688 20%/创业板 20%/主板 10%) | 常量，零依赖 |
| limits.py | `get_limit_prices`(:8)、`_round2_half_up`(:27) | 涨跌停价：优先 bar 列，否则 pre_close×板块幅度，Decimal ROUND_HALF_UP 到分 |
| costs.py | `make_costs_fn`(:4) | 费用闭包工厂：佣金（最低 5 元）/印花税（仅卖）/过户费 |
| slippage.py | `apply_slippage`(:4) | tick 滑点：price ± ticks×0.01 |
| corporate.py | `adjust`(:15) | 除权除息：送股股数/价格重缩放、现金分红（除息日预缴红利税 ≤30d 20%/≤1y 10%/>1y 免） |
| filters.py | `StockFilter`(:25)、`filter_required_columns`(:13) | 见 2.3 |
| database.py | `init_backtest_db`(:87，含 stats_json 轻量迁移）、write_run(:114)/write_daily(:125)/write_trade(:156)/write_run_stats(:190)/read_runs(:198)/read_run_data(:203)/write_ml_predictions(:223)/write_debug_snapshot(:234) | 结果库 SQLite：runs/account_daily/holdings/trade_log/debug_snapshots/ml_predictions 六表 |
| stats.py | `calculate_statistics`(:9) | 统计指标纯函数：收益/回撤/夏普/Sortino/VaR/回合 FIFO(:216)/卖出来源归因(:374)/成本分解(:439)/交易磨损(:459)/管理复杂度(:505)/基准对比(:557) |

---

## 3. 用户层与研究层

### 3.1 adapters/ —— tushare.py

`TushareBackend`（adapters/tushare.py:230）= GenericSQLBackend + 表单：
stk_factor_pro（行情）/ trade_cal（日历）/ dividend（分红）/ stock_st / index_weight /
idx_factor_pro（基准）等表映射 + aux_tables（moneyflow/cyq_perf/margin_detail）LEFT JOIN。
填了能力空位即装配对应鸭子类型方法。

### 3.2 factors/library.yaml

88 个因子，纯数据：`- name: {expr, where?, description}`。引用基础列（裸价/复权/扩展列
如 turnover_rate/pe_ttm/total_mv）与其他因子名构成 DAG。禁止出现 `btcore/factors/builtin.py`
（反破坏 linter 检查）——引擎只提供表达式机制，不内置任何具体因子。

### 3.3 strategies/

- `examples/`：教学参考 —— bare_bones（最小骨架）→ rolling_ranker（因子轮动+冷却期+
  buy_weights）→ condition_hunter（条件买入）→ target_allocator（target_value 调仓）→
  self_managed_time / self_managed_rank（自管理调仓节奏）→ multi_model（ML 双 scope）
- `selected/`（trend_guard 等）、`exploring/`（ml_alpha、signal_fusion）、`archive/`
- 策略 = `config.yaml`（strategy: module:Class + factor_specs + filter_rules + conditions +
  models + config）+ `strategy.py`（Strategy 子类）

### 3.4 research/（纯 importable，无 CLI）

| 模块 | 关键符号 | 职责 |
|---|---|---|
| factor_eval.py | `calc_ic`(:10) / `calc_layered_returns`(:42) / `summarize_ic`(:80) / `calc_factor_corr`(:102) / `calc_ic_decay`(:134) | 因子 IC/分层/相关性/衰减 |
| composite.py | `combine_factors`(:29) / `evaluate_composite`(:80) | 多因子滚动 IC/ICIR 加权合成 |
| attribution.py | `brinson_attribute`(:508) / `brinson_attribute_from_files`(:657) | Brinson 行业归因（日度持仓重建 :177） |
| report.py | `load_runs`(:73) / `generate_report`(:493) / `generate_compare_report`(:575) / `build_compare_table`(:562) | 单文件 HTML 报告与多 run 对比（内联 SVG，无前端依赖） |

### 3.5 scripts/（CLI 入口）

| 脚本 | main | 用途 |
|---|---|---|
| run.py | :38 | YAML 策略回测 → result.db |
| report.py / compare.py | :15 / :28 | 单 run HTML 报告 / 多 run 对比表+HTML |
| factor_eval.py | :64 | 因子 IC/分层/相关性（--model 可评 ML 分数列） |
| ml_train.py | :66 | ML 训练（panel/holding 双 scope，同一物化路径） |
| sweep.py | :47 | 参数扫描批量回测（点路径语法展开参数空间） |
| replay.py | :10 | 交易决策回放（消费 debug_snapshots） |
| cross_validate.py | :213 | 回测结果交叉验证（validate_trades :56 / validate_daily :178） |
| check_anticorrupt.py | :205 | 反破坏 linter（7 项结构检查，提交前必过） |
| dump_fixtures.py / dump_brinson_data.py | :54 / :10 | fixtures 再生成 / 归因数据导出 |
| smoke_test_all.py | :523 | 改进项冒烟测试集 |
| bench_universe_preload.py | :70 | universe preload 性能基准 |

---

## 4. 数据流

### 4.1 run() preload 序列（engine.py:113-240，代码顺序）

```
init_backtest_db → get_calendar → get_factor_universe / get_universe
→ _build_factor_plan（warmup = fplan.main_days 或 365 天）
→ get_engine_bars(load_symbols, columns=required_bar_columns(...))   ← 列裁剪生效点
→ validate_required_columns（fail-fast）→ derive_fields（hfq/pct_chg）
→ 因子物化：_preload_breadth → ensure_pseudo_columns → materialize → validate_materialization
→ ML panel 物化：ml_runtime.materialize_predictions → bars_df['ml_<name>']   ← 必须在裁切前
→ factor_universe 裁切到交易域（裁空 → ValueError）
→ bars_by_date 懒切片（_DaySlicer，不复制面板）→ provider.attach_bars → strategy.on_start
→ write_run（独立事务，status=running）
→ _compute_pending(prev_day)   ← 首日信号在前一交易日预计算（T 信号 T+1 撮合）
→ for today in calendar: step(today, day_bars, conn)
→ stats.calculate_statistics → write_run_stats + status=completed（异常 → failed）
```

### 4.2 step() 单日序列（engine.py:291-363，代码顺序）

```
_bars_to_dict → _save_state（事务回滚快照）
→ corporate.adjust                    除权除息
→ 撮合（全部 T+1：执行昨日 pending）：
    target_value → rebalance_to_targets
    否则 manual_sell → manual_buy
    → exit_conditions（条件卖）
    → entry_conditions（条件买，最后执行吃当日释放现金）
→ _settle                             估值 + 写库
→ _compute_pending(today)             算次日 pending：
    provider._as_of_date = today      ← 前视钳制锚点
    持仓 holding_days+1, locked=False（T+1 解锁）
    → holding 模型分数注入 → on_fills → on_tick → select → 返回协议校验
    → 逐持仓 calc_conditions
→ _write_ml_predictions / _write_debug_snapshot（可选）
异常 → _restore_state + 重抛
```

### 4.3 select 返回协议（engine.py:404-541 校验）

`select()` 返回 dict，键：`buy` / `sell` / `target_value` / `sell_shares` / `buy_weights` /
`buy_conditions`。互斥与校验：buy∩sell=∅；target_value ⊥ buy/sell；buy_weights 键==buy、
单项∈(0,1]、和≤1；buy_conditions 必填 {symbol,type,price}、value/shares 恰一、类型须已注册。

---

## 5. 约定（跨模块不变式）

| 约定 | 内容 |
|---|---|
| 日期格式 | 全仓 `YYYYMMDD` str；面板 MultiIndex(trade_date, symbol) |
| 价格体系 | 撮合/成本/估值用**裸价**（open/close/high/low）；因子/排名用**后复权**（*_hfq = 裸价 × adj_factor，由 derive_fields 物化为 *_hfq 列）。不可混用 |
| T+1 | 买入当日 `Holding.locked=True`，次日 _compute_pending 解锁；锁定期间条件单跳过 |
| 信号-撮合错期 | T 日 select/条件单声明 → T+1 日 step 撮合（首日信号在 prev_day 预计算） |
| 前视屏蔽 | ①因子一次性因果物化（滚动窗口仅用 ≤ 当日）；②provider 查询按 _as_of_date 钳制到前一交易日；③T 信号 T+1 撮合。约定性保护，无 GuardedProvider 包装器 |
| 软回退 vs Fail-Fast | 可选能力缺失（ST/行业/指数成分表）→ 告警后继续；明确依赖缺失（必需列/因子名/表单引用/meta v2）→ 加载或 preload 直接 ValueError |
| 鸭子类型 | backend 能力方法、strategy.on_fills/on_tick 均 getattr 探测，缺失降级 |
| materialize_only | factor_specs 标记位：物化为列供 calc_conditions/模型特征读取，不参与评分；loader 自动为模型 features 追加 |
| ML 分数列 | `ml_<name>`：panel scope 物化为面板列（裁切前）；holding scope 决策时点注入 bar dict。策略不得自行加载 ONNX 逐日推理 |
| 财报对齐 | 引擎只消费 (交易日,代码) 日频网格列，不做季度推断；公告日对齐由后端在数据层完成 |
| 事务模型 | write_run 独立事务；每 step 一个 `with conn`，异常 _restore_state 回滚账户态；崩溃标 failed |
| 结果库 | 多 run 累积 SQLite，6 表（runs/account_daily/holdings/trade_log/debug_snapshots/ml_predictions）；holdings 为瞬态快照每 run 清空；stats_json 缺列时 ALTER TABLE 轻量迁移 |

---

## 6. 测试

- `tests/conftest.py:66 MockDataBackend`：从 `tests/fixtures/*.parquet`（10 个文件，已提交）
  读数据，完整复刻 DataBackend 接口；helper：make_holding/make_account/make_bar
- `tests/test_invariants/` 8 个不变量（16 测试，手动步进引擎）：
  INV1 账户恒等式 / INV2 手数整百 / INV3 现金非负 / INV4 T+1 锁定 /
  INV5 买卖互斥 / INV6 公司行为一致性 / INV7 条件单成交价∈[low,high] / INV8 涨跌停跳过
- 其余 35 个顶层测试文件（391 测试）按域分布：引擎撮合、成本/限制/公司行为、数据后端
  （generic_sql 34 个）、因子系统（ops/plan/cse/library/eval）、策略加载、统计/结果库/报告、
  ML（test_ml.py 27 个，含训练面板与引擎物化一致性）
- 命令：`pytest tests/ -v`；`ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/`；
  `python scripts/check_anticorrupt.py`

---

## 7. 文档地图

| 文档 | 内容 |
|---|---|
| docs/index.md | 导航入口、能力全景表、设计原则 |
| docs/backend_guide.md | 填表法、数据契约口径、能力空位、扩展字段 |
| docs/factor_library.md | 算子表达式、DAG、物化与 CSE、因子评估与合成 |
| docs/strategy_guide.md | 策略 YAML 完整参考、Python 接口、条件单、debug 模式 |
| docs/cli_and_research.md | CLI 用法、归因、合成因子 |
| docs/ml_guide.md | ML 双 scope、训练部署同源物化、ONNX 推理 |
