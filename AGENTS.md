# AGENTS.md — ddup

A 股日频量化策略回测引擎。Python 3.12+，uv + hatchling 管理。

> 用户可编辑的目录：`adapters/`（后端实现）、`factors/`（因子定义）、
> `strategies/`（策略实现）。所有机制/基础设施在 `btcore/`。详见 `docs/user_guide.md`。

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
python scripts/run.py strategies/examples/topk_momentum/config.yaml --start 20240603 --end 20240628

# 从结果库生成单 run HTML 报告 / 多 run 对比
python scripts/report.py result.db --out report.html
python scripts/compare.py result.db --html compare.html

# 因子 IC 评估 / 分层回测 / 相关性矩阵
python scripts/factor_eval.py mom20,vol_z --start 20240101 --end 20240630

# 从真实数据库重新生成测试 fixtures
python scripts/dump_fixtures.py
```

---

## 架构分层与依赖规则（反破坏 linter 强制检查）

```
btcore/     — 全部机制/基础设施（引擎、ABC、因子库机制、策略加载器/工具）
              不要随意修改
adapters/   — 用户数据后端实现（可编辑；通常是对 GenericSQLBackend 的填表）
research/   — 研究工具（因子评估、归因、HTML 报告与多 run 对比）
factors/    — 用户因子定义（library.yaml，可编辑）
strategies/ — 用户策略（YAML + Strategy 子类；可编辑）
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

## 禁止重引入的设计

以下模式已从代码库中移除，绝对不要重新引入：

- **`factors/builtin.py` 不得存在**：引擎只提供表达式机制，不内置任何具体因子
- **因子类层次**：`Factor` / `CrossSection` / `ExprFactor` / `FunctionFactor` /
  `StrategyAdapter` / `FactorSpecItem` / `FactorPipeline` / `equal_weight_percentile` /
  `register_factor` 已删除，因子定义是纯 YAML 数据
- **Strategy ABC 不得含行为开关**：只有声明式属性 `REQUIRED_FIELDS` / `FACTOR_SPECS` /
  `FILTER_RULES`。不得有 `take_profit_mode` / `trailing_conservative` 等行为分支
- **因子算子运行时注册表**：算子表 `_OPS` 是固定 dict，不是可扩展注册表。
  自定义条件单 handler 通过 `register_condition_handler` / `register_buy_condition_handler`
  注册（进程级全局），但这是条件单机制，不是因子机制
- **Holding 不得有 `last_adj_factor`**：公司行为仅用分红表，不做启发式兜底
- **GuardedProvider 包装器不得存在**：单一 provider 对象，前视保护是约定性的

---

## 关键机制索引

详细设计见对应文档，这里仅提供一句话定位：

| 机制 | 位置 | 文档 |
|---|---|---|
| 因子 DAG 模型与算子 | `btcore/factors/ops.py` | `docs/factor_library.md` |
| 因子物化规划与两路供给 | `btcore/factors/plan.py` | `docs/factor_library.md` |
| 物化公共子表达式消除（CSE） | `btcore/factors/cse.py` | `docs/factor_library.md` |
| 多因子合成（滚动 IC/ICIR 加权） | `research/composite.py` | `docs/factor_library.md` |
| 策略 YAML 加载 | `btcore/strategy_loader.py` | `docs/strategy_guide.md` |
| select 返回协议与冲突校验 | `btcore/engine.py:_compute_pending` | `docs/strategy_guide.md` |
| 填表法后端 | `btcore/generic_sql.py` | `docs/backend_guide.md` |
| 条件单 dispatch 与自定义注册 | `btcore/match/conditions.py` | `docs/strategy_guide.md` |
| 组合风控（熔断/仓位/行业上限） | `btcore/risk.py` | `docs/strategy_guide.md` |
| 调仓调度 | `btcore/strategy_tools.py:wrap_strategy` | `docs/strategy_guide.md` |
| 滑点模型（tick=0.01） | `btcore/slippage.py` | `docs/strategy_guide.md` |
| 成本函数（可配置费率） | `btcore/costs.py` | `docs/strategy_guide.md` |
| 多 run 结果库 schema | `btcore/database.py` | `docs/strategy_guide.md` |
| 单文件 HTML 报告与多 run 对比（内联 SVG） | `research/report.py` | `docs/strategy_guide.md` |
| 涨跌停价格推算（ROUND_HALF_UP） | `btcore/limits.py` | — |
| 撮合入口（价格校验/成交量cap等） | `btcore/match/core.py` | — |
| 列裁剪推导 | `btcore/engine.py:required_bar_columns` | `docs/strategy_guide.md` |

---

## 测试

- `MockDataBackend`（`tests/conftest.py`）从 parquet fixtures 读取数据，完全复刻 DataBackend 接口
- Fixtures 在 `tests/fixtures/*.parquet`（约 2.8MB，已提交 git）
- 8 个不变量测试：`tests/test_invariants/`（INV1 账户恒等式、INV2 手数、INV3 现金非负、
  INV4 T+1 锁定、INV5 买卖互斥、INV6 公司行为一致性、INV7 条件单成交价范围、INV8 涨跌停跳过）
- 355 个测试总计，覆盖因子库、策略层、target_value、volume-ratio、scheduler、fill-notification、
  列裁剪、风控规则、index_universe、因子算子、物化规划与 CSE、多因子合成、卖出来源归因、
  GenericSQLBackend 表单校验、
  统计指标（交易磨损/管理复杂度）、HTML 报告与多 run 对比、stats_json 落盘迁移等

---

## 风格

- Ruff：line-length=100，选择规则 E/F/I/N/W，双引号，通过 I 规则启用 isort
- 类型标注使用，Python 3.12+ 语法
- Docstring 最小化；设计文档在 `docs/`
- 代码与注释中不使用 emoji
- 编辑任何文件后检查是否影响同目录下的 `AGENTS.md`
