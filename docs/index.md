# ddup 文档

面向 A 股的日频量化研究与回测引擎。因子以 YAML 声明，策略以 Python 子类编写，行情数据以表单映射接入自有数据库。引擎负责撮合、费用、滑点、统计与全链路前视屏蔽；研究与回测共享同一因子计算路径。

## 目录分工

```
btcore/      — 引擎内核（机制，不直接编辑）
adapters/    — 数据后端：你的数据库接在这里
factors/     — 因子定义：纯 YAML，无类、无注册表
strategies/  — 策略实现：Python 子类 + YAML 配置
research/    — 研究工具：纯函数 API，不依赖引擎
scripts/     — CLI 入口：回测、报告、评估、扫描、回放
```

## 使用者必须知道的行为契约

以下契约决定回测结果如何产生，编写数据后端、因子或策略前应先理解。

| 契约 | 规则 |
|---|---|
| 价格体系 | 撮合、成本、估值用裸价（open/close/high/low）；因子计算、排名用后复权（`*_hfq`，由裸价 × adj_factor 派生）。两套价格不可混用 |
| T+1 锁定 | 买入当日持仓锁定，次日解锁；锁定期间条件单跳过该持仓 |
| T 日信号 T+1 执行 | select() 当日产出名单，次日按 execution_price（open/close）撮合；条件单 T 日声明，T+1 盘中触发 |
| 前视屏蔽 | 因子在回测启动时一次性物化为因果列（仅用 ≤ 当日数据）；数据查询按当前模拟日钳制；研究工具与回测共用同一计算路径 |
| 软回退 vs Fail-Fast | 可选数据能力缺失（ST/行业/指数成分/基准）→ 告警一次，对应规则不生效，继续运行；明确声明的依赖缺失（因子名不存在、引用的伪列无后端支持、必需列缺失）→ 加载阶段直接报错 |
| 能力开关 | 辅助能力（ST 标记、行业、上市日期、指数成分、基准）以后端是否提供对应数据为开关，无需额外配置 |

## 能力全景

| 我要做什么 | 位置 | 文档 |
|---|---|---|
| 对接行情数据库 | `adapters/` 填表（或实现 DataBackend） | [后端对接指南](./backend_guide.md) |
| 接入财报/资金流等扩展字段 | 表单 `extra_fields` | [后端对接指南](./backend_guide.md) |
| 定义因子 | `factors/library.yaml` | [因子库指南](./factor_library.md) |
| 因子 IC / 分层 / 相关性 / 衰减评估 | `scripts/factor_eval.py`、`research.factor_eval` | [因子库指南](./factor_library.md) |
| 多因子合成（等权 / IC / ICIR 加权） | `research.composite` | [CLI 与研究工具](./cli_and_research.md) |
| 编写策略买卖逻辑 | `strategies/` 继承 Strategy | [策略设计指南](./strategy_guide.md) |
| 过滤股票（ST/新股/板块/行业/价格/指数池） | YAML `filter_rules` | [策略设计指南](./strategy_guide.md) |
| 止损止盈 / 移动止盈 / 自定义离场 | YAML `conditions` + ConditionBuilder | [策略设计指南](./strategy_guide.md) |
| 条件买入（限价回踩 / 突破追涨） | select 返回 `buy_conditions` | [策略设计指南](./strategy_guide.md) |
| 目标仓位精确调仓 / 部分减仓 | select 返回 `target_value` / `sell_shares` | [策略设计指南](./strategy_guide.md) |
| 配置费率、滑点、基准、成交量约束 | YAML `config` 引擎键 | [策略设计指南](./strategy_guide.md) |
| 训练 ML 模型打分（截面 / 持仓双 scope） | YAML `models` 节 + `scripts/ml_train.py` | [ML 子系统指南](./ml_guide.md) |
| 运行回测 | `scripts/run.py` | [CLI 与研究工具](./cli_and_research.md) |
| 参数扫描批量回测 | `scripts/sweep.py` | [CLI 与研究工具](./cli_and_research.md) |
| HTML 报告 / 多 run 对比 | `scripts/report.py`、`scripts/compare.py` | [CLI 与研究工具](./cli_and_research.md) |
| 交易合理性交叉验证 | `scripts/cross_validate.py` | [CLI 与研究工具](./cli_and_research.md) |
| 交易决策回放调试 | debug 模式 + `scripts/replay.py` | [策略设计指南](./strategy_guide.md) |
| Brinson 行业归因 | `research.attribution` | [CLI 与研究工具](./cli_and_research.md) |
| 程序化 API（Python 驱动回测） | `Engine(strategy, provider).run()` | [策略设计指南](./strategy_guide.md) |
| 让 Agent 自主做策略研究 | `.omp/skills/` 研究 skills（随仓库分发） | `ddup-research-loop` 入口共 6 个，agent 按任务触发加载 |

## 标准工作流

1. **对接数据**（一次性）：在 `adapters/` 填表映射自有数据库 → [后端对接指南](./backend_guide.md)
2. **定义因子**：在 `factors/library.yaml` 登记表达式，用 `factor_eval.py` 验证 IC 与分层区分力 → [因子库指南](./factor_library.md)
3. **编写策略**：在 `strategies/` 建 YAML + Python 子类，从 `strategies/examples/` 的逐级示例起步 → [策略设计指南](./strategy_guide.md)
4. **（可选）训练 ML 模型**：YAML 声明 `models`，`ml_train.py` 训练，策略消费 `ml_<name>` 分数 → [ML 子系统指南](./ml_guide.md)
5. **回测迭代**：`run.py` 运行 → `cross_validate.py` 检查交易合理性 → `report.py` / `compare.py` 分析绩效 → [CLI 与研究工具](./cli_and_research.md)

## 文档索引

- **[后端对接指南](./backend_guide.md)** — 填表法表单结构、十三必需接口、辅助能力空位、扩展字段、tables 节、能力缺失行为速查
- **[因子库指南](./factor_library.md)** — 因子定义格式、两条求值路径、算子全集、命名引用与 DAG、where 子句、可用列速查、策略消费、因子评估
- **[策略设计指南](./strategy_guide.md)** — Strategy 接口参考、YAML 完整参考、条件单系统、撮合与执行、Level 0-4 教程、进阶模式、结果库 schema、速查表全集
- **[ML 子系统指南](./ml_guide.md)** — panel/holding 双 scope、models 配置、训练命令、meta 契约、model_exit、可观测性
- **[CLI 与研究工具](./cli_and_research.md)** — 全部 CLI 参数表、研究库 API（因子评估/报告/合成/Brinson）、典型工作流、速查表

Agent 自主研究指导在 `.omp/skills/`（随仓库分发）：`ddup-research-loop` 元流程入口，路由至因子/策略/实验/分析/ML 五个专题 skill；与代码的事实同步由 `scripts/check_skill_sync.py` 强制。

开发相关规范（架构分层、依赖规则、测试）见项目根目录 `AGENTS.md` 与 `ARCHITECTURE.md`。
