# AGENTS.md — ddup

A 股日频量化策略回测引擎。Python 3.12+，uv + hatchling 管理。

> 用户可编辑的目录：`adapters/`（后端实现）、`factors/`（因子定义）、
> `strategies/`（策略实现）。所有机制/基础设施在 `btcore/`。
> 详见 `docs/index.md`（导航入口）、`docs/backend_guide.md`、`docs/factor_library.md`、
> `docs/strategy_guide.md`、`docs/cli_and_research.md`。

---

## 命令

```bash
# 全部测试（仅用 fixtures，不需要真实数据库）
pytest tests/ -v

# 覆盖率
pytest tests/ --cov=btcore -v

# Lint + 格式化（ruff，line-length=100，双引号，规则 E/F/I/N/W）
ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/

# 反破坏 linter（提交前必须通过）
python scripts/check_anticorrupt.py

# 运行 YAML 策略回测（需要真实数据库）
python scripts/run.py strategies/examples/rolling_ranker/config.yaml --start 20240603 --end 20240628

# 从结果库生成单 run HTML 报告 / 多 run 对比
python scripts/report.py result.db --out report.html
python scripts/compare.py result.db --html compare.html

# 因子 IC 评估 / 分层回测 / 相关性矩阵（--model 可评 ML 模型分数）
python scripts/factor_eval.py mom20,vol_z --start 20240101 --end 20240630

# ML 模型训练（panel scope 截面模型 / holding scope 持仓模型，需要真实数据库）
python scripts/ml_train.py strategies/my_strategy/config.yaml --model alpha_xs --start 20220101 --end 20250630 --horizon 5

# 回测结果交叉验证（交易行为、磨损合理性检查）
python scripts/cross_validate.py result.db --strategy name --run-id 1

# skill 与代码事实同步校验（改 CLI/算子/YAML 键/协议后必跑）
python scripts/check_skill_sync.py

# 参数扫描批量回测（YAML 路径语法展开参数空间）
python scripts/sweep.py sweep.yaml --start 20240101 --end 20240630 --out sweep.db

# 交易决策回放（按 symbol/日期定位调试上下文）
python scripts/replay.py result.db --symbol 000001.SZ --date 20240605

# 实盘账本：建账 / 每日对账同步 / 明日操作单 / 状态（账本与策略解耦，可换任意策略 YAML）
python scripts/live.py init live/main.db --date 20260731 --cash 40000 [--positions p.yaml]
python scripts/live.py sync live/main.db sync.yaml
python scripts/live.py signal live/main.db strategies/selected/trend_guard_bw_300/config.yaml --date 20260731
python scripts/live.py status live/main.db

# Brinson 归因数据导出（一次性导出 parquet，后续离线归因）
python scripts/dump_brinson_data.py /path/to/tushare.db --out brinson_data

# 从真实数据库重新生成测试 fixtures
python scripts/dump_fixtures.py
```

---

## 架构分层与依赖规则（反破坏 linter 强制检查）

```
btcore/     — 全部机制/基础设施（引擎、ABC、因子库机制、策略加载器/工具、
              ML 子系统 btcore/ml）
              不要随意修改
adapters/   — 用户数据后端实现（可编辑；通常是对 GenericSQLBackend 的填表）
research/   — 研究工具库（纯 importable 模块，不含 CLI；因子评估、归因、
              HTML 报告生成、多因子合成、实盘账本回放 research/live.py）
scripts/    — 可执行 CLI 入口（回测运行、报告/对比、因子评估、交叉验证、
              性能基准、fixtures 生成、反破坏检查）
factors/    — 用户因子定义（library.yaml，可编辑）
strategies/ — 用户策略（YAML + Strategy 子类；可编辑）
.omp/skills/ — agent 研究指导 skills（随仓库分发；接口变更须同步）
```

必须遵守：

- `types.py` / `constants.py`：零依赖（被所有人依赖）
- `match/*`（manual.py / conditions.py / core.py）：子模块之间不允许互相 import，仅可依赖 core.py 工具
- `stats.py`：纯函数，不含 sqlite3 import，不依赖 provider / engine
- `factors/*`：不得依赖 engine / match / database / provider
- `engine.py`：不允许被 btcore 内部模块 import（仅用户代码调用）
- `btcore/` 不得 import `strategies/`、顶层 `factors/`、`adapters/`——单向依赖
- 顶层 `factors/` 仅可依赖 `btcore.factors`（不得依赖 strategies / research / adapters / scripts）
- 全局无循环 import

---

## 设计契约（跨模块不变式）

以下规则是引擎设计的基石，任何模块的修改都不能违反。它们不是实现细节，而是架构级约定。

| 契约 | 规则 |
|------|------|
| **价格体系** | 撮合、成本、估值使用裸价（`open` / `close` / `high` / `low`）。因子计算、排名使用后复权（`open_hfq` / `close_hfq` 等，公式 `x × adj_factor`）。**不可混用**——裸价做排名会导致除权除息日股价跳空被误判为涨跌信号 |
| **T+1 锁定** | 买入当日 `Holding.locked = True`，次日解锁。锁定期间条件单跳过该持仓——不会出现当天买入当天止损卖出 |
| **软回退 vs Fail-Fast** | 可选能力缺失（ST 表、行业表、指数成分表）→ 引擎告警后继续运行，对应规则不生效。明确声明的依赖缺失（因子伪列无后端、必需列缺失、因子名不存在、表单引用不存在）→ 加载或 preload 阶段直接报错，不产生静默错误结果 |
| **前视屏蔽** | 三重保护：①因子 preload 一次性物化为因果列（滚动窗口与截面聚合仅用 ≤ 当日数据）；②`DataProvider` 所有查询按当前模拟日钳制；③T 日信号 T+1 撮合，条件单 T 日声明 T+1 盘中触发 |
| **财报数据对齐** | 引擎只消费 `(交易日, 代码)` 日频网格上的列，**不做季度频率推断**。财报类数据须由后端在数据层按公告日（而非报告期）对齐成日频列。跨季度运算（如 YoY）须预先物化为列 |

---

## 禁止重引入的设计

以下模式已从代码库中移除，绝对不要重新引入：

- **`factors/builtin.py` 不得存在**：引擎只提供表达式机制，不内置任何具体因子
- **因子类层次**：`Factor` / `CrossSection` / `ExprFactor` / `FunctionFactor` /
  `StrategyAdapter` / `FactorSpecItem` / `FactorPipeline` / `equal_weight_percentile` /
  `register_factor` 已删除，因子定义是纯 YAML 数据
- **Strategy ABC 不得含行为开关**：只有声明式数据属性（`REQUIRED_FIELDS` /
  `FACTOR_SPECS` / `FILTER_RULES` / `FACTOR_NODES` / `MODEL_SPECS` /
  `CONDITION_FACTORS`），不得含 `take_profit_mode` / `trailing_conservative`
  等行为分支开关
- **因子算子运行时注册表**：算子表 `_OPS` 是固定 dict，不是可扩展注册表。
  自定义条件单 handler 通过 `register_condition_handler` / `register_buy_condition_handler`
  注册（进程级全局），但这是条件单机制，不是因子机制
- **Holding 不得有 `last_adj_factor`**：公司行为仅用分红表，不做启发式兜底
- **GuardedProvider 包装器不得存在**：单一 provider 对象，前视保护是约定性的
- **ML 外挂模式不得存在**：`MLConfig` / `MLEvaluator` / `config["_ml_config"]` /
  factor_specs 的 `type: ml_feature` 已删除。模型经策略 YAML `models` 节声明，
  对引擎是意图中性的打分公式——scope=panel（无账户态特征）在 preload
  物化为 `ml_<name>` 列；scope=holding 在决策时点求值注入持仓 bar。
  分数的解释权在策略（factor_specs / conditions.model_exit / 自读），
  引擎不得硬编码模型意图（如已删除的 exit_guard role/threshold）；
  策略不得自行加载 ONNX 做逐日推理（绕开前视保护与物化体系）

---

## 策略设计五要素

编写策略前，按以下五要素逐项填空，填不满的格子就是口头规则里靠临场发挥的地方——那些不补上就无法真正程序化。

五要素说明书同时充当两个角色：对引擎是执行规格，对人是回测审查时的核对底稿。

### 1. 信号周期

**先定多久做一次判断，再选数据粒度。** 决策频率和数据粗细是两层。

策略在 `select()` 中自行管理调仓节奏（时间门控、排名阈值等模式），而非依赖引擎声明式拦截。`select()` 每日运行，非调仓日返回空名单即可；`calc_conditions` 同样每日运行，不受策略内部调仓判断影响。

参见 `strategies/examples/self_managed_time/`、`self_managed_rank/` 的自管理模式参考实现。

### 2. 入场条件 — 过滤 + 触发两层

**过滤回答「做多/做空/双向」，触发回答「什么价格动作下单」。** 混成一句「看起来该买了」，回测很难核对。

| 层 | 职责 | ddup 映射 |
|---|---|---|
| 方向过滤 | 只做多/只做空/双向 | `filter_rules`（ST/板块/亏损/价格）+ `factor_specs.ascending` |
| 触发 | 具体下单时点与价格 | `select()` 返回 buy_list + 条件买入（`BREAKOUT_BUY` / `LIMIT_BUY`） |

- 趋势过滤 + 突破触发是推荐范式：过滤器负责少做逆势单，触发条件负责给出具体下单时点
- 过滤逻辑应集中在 `StockFilter` 或 `select()` 的过滤阶段，不要散落在各处

### 3. 出场条件 — 至少一条离场路径

**只写进场、不写出场，规则只完成了一半。** 多路径时必须写清优先级。

- ddup 映射：`conditions`（声明式）+ `calc_conditions()`（程序式），两条路径等价
- 内置规则：`stop_loss_pct`（固定止损）、`take_profit_pct`（固定止盈）、`trailing_pct`（移动止盈）
- 优先级：`exit_conditions()` 按 `holding.conditions` 列表顺序，首条触发即 `break`
- 自定义离场通过 `register_condition_handler` 注册
- **`calc_conditions` 返回空列表技术上合法，但意味策略没有离场计划——审查时应标记为不完整**

### 4. 仓位规则 — 先固定，后优化

**固定手数/等权买入能把「逻辑对不对」和「仓位大不大」分开。** 变量越少，回测核对参照越清晰。

- ddup 映射：`config.max_positions` + `top_k` + `buy_weights`
- 入门建议：等权买入（不设 `buy_weights`）或简单 top_k 轮动，逻辑稳定后再引入按波动/资金比例加减仓
- 引擎层自动兜底：`cap_by_volume`（成交量约束）

### 5. 边界情况 — 「不交易」本身就是规则

每一处「到时候看」都必须在说明书里有明确答案：

| 边界情况 | ddup 处理 | 策略层需确认的点 |
|---|---|---|
| 方向不明 / 条件未触发 | `select()` 返回 `{"buy": [], "sell": []}` | 空名单是预期行为，不是异常 |
| 已有持仓又见同向信号 | 引擎不自动加仓 | 策略必须显式决定：忽略 / 加仓 / 禁止 |
| 已有持仓又见反向信号 | 引擎不自动对锁 | 如需「反向信号平仓」，写进 `calc_conditions` |
| 涨跌停 / 成交量不足 | `entry_conditions` / `exit_conditions` 自动跳过并 warn | 策略应知晓这是静默跳过，不算成交 |

---

## 关键机制索引

详细设计见对应文档，这里仅提供一句话定位：

| 机制 | 位置 | 文档 |
|---|---|---|
| 因子 DAG 模型与算子 | `btcore/factors/ops.py` | `docs/factor_library.md` |
| 因子物化规划与两路供给 | `btcore/factors/plan.py` | `docs/factor_library.md` |
| 物化公共子表达式消除（CSE） | `btcore/factors/cse.py` | `docs/factor_library.md` |
| 多因子合成（滚动 IC/ICIR 加权） | `research/composite.py` | `docs/cli_and_research.md` |
| ML 子系统（panel/holding 双 scope、同源物化、model_exit、meta v3 契约） | `btcore/ml/` | `docs/ml_guide.md` |
| 因子评估（IC/分层/相关性） | `research/factor_eval.py` | `docs/cli_and_research.md` |
| Brinson 行业归因 | `research/attribution.py` | `docs/cli_and_research.md` |
| 策略 YAML 加载 | `btcore/strategy_loader.py` | `docs/strategy_guide.md` |
| select 返回协议与冲突校验 | `btcore/engine.py:compute_pending` | `docs/strategy_guide.md` |
| 填表法后端 | `btcore/generic_sql.py` | `docs/backend_guide.md` |
| 条件单 dispatch 与自定义注册 | `btcore/match/conditions.py` | `docs/strategy_guide.md` |
| 滑点模型（tick=0.01） | `btcore/slippage.py` | `docs/strategy_guide.md` |
| 成本函数（可配置费率） | `btcore/costs.py` | `docs/strategy_guide.md` |
| 多 run 结果库 schema | `btcore/database.py` | `docs/strategy_guide.md` |
| 单文件 HTML 报告与多 run 对比（内联 SVG） | `research/report.py` | `docs/cli_and_research.md` |
| 回测 CLI（run/report/compare/交叉验证/sweep/replay） | `scripts/` | `docs/cli_and_research.md` |
| debug 快照与交易回放 | `btcore/database.py`, `scripts/replay.py` | `docs/strategy_guide.md`, `docs/cli_and_research.md` |
| 实盘账本与回放（ledger 驱动、策略解耦、明日操作单） | `research/live.py`, `scripts/live.py` | `docs/cli_and_research.md` |
| 坍缩因子流式计算（compute_breadth） | `btcore/factors/library.py` | `docs/factor_library.md` |
| 涨跌停价格推算（ROUND_HALF_UP） | `btcore/limits.py` | — |
| 撮合入口（价格校验/成交量cap等） | `btcore/match/core.py` | — |
| 列裁剪推导 | `btcore/engine.py:required_bar_columns` | `docs/strategy_guide.md` |

---

## 测试

- `MockDataBackend`（`tests/conftest.py`）从 parquet fixtures 读取数据，完全复刻 DataBackend 接口
- Fixtures 在 `tests/fixtures/*.parquet`（约 2.8MB，已提交 git）
- 8 个不变量测试：`tests/test_invariants/`（INV1 账户恒等式、INV2 手数、INV3 现金非负、
  INV4 T+1 锁定、INV5 买卖互斥、INV6 公司行为一致性、INV7 条件单成交价范围、INV8 涨跌停跳过）
- 539 个测试总计，覆盖因子库、策略层、target_value、volume-ratio、fill-notification、
  列裁剪、index_universe、因子算子、物化规划与 CSE、多因子合成、卖出来源归因、
  GenericSQLBackend 表单校验、
  ML 子系统（spec 解析、loader 整合、panel/holding 双 scope 引擎集成、T+1 锁定、
  训练面板与引擎物化一致性、时间切分 embargo、评估指标）、
  统计指标（交易磨损/管理复杂度）、HTML 报告与多 run 对比、stats_json 落盘迁移、
  debug 快照与回放、参数扫描、坍缩因子物化完整性、on_tick 条件买单、factor_plan 验证、
  实盘账本（成交应用/对账/操作单/回测往返一致性 parity）、select 协议 sell_reasons 键

---

## 风格

- Ruff：line-length=100，选择规则 E/F/I/N/W，双引号，通过 I 规则启用 isort
- 类型标注使用，Python 3.12+ 语法
- Docstring 最小化；设计文档在 `docs/`
- 代码与注释中不使用 emoji
- 编辑任何文件后检查是否影响同目录下的 `AGENTS.md`
- 接口变更（CLI 参数、算子、YAML 键、select 协议、config 默认值、ML meta 契约）
  必须同批更新 `.omp/skills/` 与 `docs/`；`scripts/check_skill_sync.py` 机械对账，
  漂移即失败
