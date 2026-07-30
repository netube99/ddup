# ddup 文档

面向 A 股的日频量化研究与回测引擎。因子以声明式 YAML 定义，策略以 Python 子类编写，行情数据库以表单映射接入。引擎负责撮合、费用、滑点、风控、统计及全链路前视屏蔽，研究与回测共享同一因子计算路径。

本文档站包含四份专题指南，覆盖从数据接入到报告生成的完整链路。

---

## 设计原则

了解这些原则有助于理解各专题指南中的设计取舍。

### 五层架构

```
btcore/          — 引擎内核（机制，不包含任何因子或策略）
adapters/        — 数据后端（你的数据库接在这里）
factors/         — 因子定义（纯 YAML，无类、无注册表）
strategies/      — 策略实现（Python 子类 + YAML 配置）
research/        — 研究工具（纯函数 API，不依赖引擎）
```

依赖单向：`strategies → btcore`、`factors → btcore.factors`、`research → btcore`。下层不导入上层，零循环依赖。

### 机制与配置分离

`btcore/` 只提供机制——数据加载、撮合、成本、风控、统计、因子物化。不含任何因子公式或买卖规则。用户代码只声明内容——数据位置、因子表达式、买卖逻辑。两层通过接口契约解耦，引擎升级不影响已有策略。

### 声明式优先

因子公式、条件单、风控阈值、调仓频率、数据位置均在 YAML 或配置 dict 中声明。仅买卖逻辑需以 Python 编写——这是策略的实质，无法声明化。

### 鸭子类型能力开关

辅助能力（ST 过滤、行业分类、指数成分、基准对比）以数据后端方法的有无为开关。填表法填写了对应空位，方法即自动装配；未填写则能力关闭。对接成本与需求精确匹配。

### 软回退与 Fail-Fast 的分界

- **可选能力缺失**（ST 表、行业表、指数成分表）：告警一次，对应规则不生效，策略继续运行。
- **明确声明的依赖缺失**（因子引用的伪列无后端支持、必需列缺失、因子名不存在）：加载或 preload 阶段直接报错，不产生静默错误。

### 前视屏蔽

- 因子在 preload 阶段一次性物化为因果列（滚动窗口与截面聚合仅用当日及之前数据）
- 数据访问通过当前模拟日钳制
- T 日信号 T+1 撮合，条件单 T 日声明 T+1 盘中触发

---

## 能力全景

| 我要做什么 | 配置位置 | 参考文档 |
|---|---|---|
| 对接行情数据 | `adapters/` 填表或实现 DataBackend | [后端对接指南](./backend_guide.md) |
| 定义因子 | `factors/library.yaml` | [因子库指南](./factor_library.md) |
| 编写策略逻辑 | `strategies/` 继承 Strategy | [策略设计指南](./strategy_guide.md) |
| 过滤股票（ST/新股/板块/价格） | YAML `filter_rules` | [策略设计指南](./strategy_guide.md) |
| 设置止损止盈条件单 | YAML `conditions` + ConditionBuilder | [策略设计指南](./strategy_guide.md) |
| 设置组合风控（熔断/仓位/行业上限） | YAML `risk_rules` | [策略设计指南](./strategy_guide.md) |
| 控制调仓频率 | YAML `schedule` | [策略设计指南](./strategy_guide.md) |
| 配置费率与滑点 | YAML `config` 引擎键 | [策略设计指南](./strategy_guide.md) |
| 条件买入（限价回踩/突破追涨） | select 返回 `buy_conditions` | [策略设计指南](./strategy_guide.md) |
| 目标仓位精确调仓 | select 返回 `target_value` | [策略设计指南](./strategy_guide.md) |
| 因子 IC/分层/相关性评估 | CLI + `research/factor_eval.py` | [因子库指南](./factor_library.md) |
| 绩效归因（Brinson） | `research/attribution.py` | [CLI 与研究工具](./cli_and_research.md) |
| 运行回测（CLI） | `scripts/run.py` | [CLI 与研究工具](./cli_and_research.md) |
| 参数扫描批量回测 | `scripts/sweep.py` | [CLI 与研究工具](./cli_and_research.md) |
| 生成 HTML 报告 | `scripts/report.py` | [CLI 与研究工具](./cli_and_research.md) |
| 多 run 参数对比 | `scripts/compare.py` | [CLI 与研究工具](./cli_and_research.md) |
| 交易决策回放调试 | `scripts/replay.py` | [CLI 与研究工具](./cli_and_research.md) |
| Brinson 本地文件归因 | `research/attribution.py` | [CLI 与研究工具](./cli_and_research.md) |
| debug 模式诊断 | `Engine(debug=True)` | [策略设计指南](./strategy_guide.md) |
| 程序式 API（Python） | `Engine(strategy, provider).run()` | [策略设计指南](./strategy_guide.md) |

---

## 工作流

**第一步：对接数据**（一次性）

在 `adapters/` 下新建文件，填写表单，初始化校验通过即可。详见 [后端对接指南](./backend_guide.md)。

**第二步：定义因子**

在 `factors/library.yaml` 中登记公式，用 CLI 工具验证 IC 和分层区分力。详见 [因子库指南](./factor_library.md)。

**第三步：编写策略**

在 `strategies/` 下创建 YAML 与 Python 文件。从 `bare_bones` 示例开始，逐级叠加条件单、风控、调度。详见 [策略设计指南](./strategy_guide.md)。

**第四步：回测迭代**

CLI 快速验证 → 交叉验证检查交易合理性 → HTML 报告分析绩效。结果库支持多轮累积存储与对比。详见 [CLI 与研究工具](./cli_and_research.md)。

---

## 文档索引

- **[后端对接指南](./backend_guide.md)** — 填表法、数据契约口径、辅助能力空位、扩展字段
- **[因子库指南](./factor_library.md)** — 算子表达式、DAG 模型、物化与 CSE、因子评估与合成
- **[策略设计指南](./strategy_guide.md)** — YAML 完整参考、Python 接口、条件单、风控、逐级教程
- **[CLI 与研究工具](./cli_and_research.md)** — 回测运行、报告生成、对比分析、Brinson 归因、合成因子

开发相关规范见项目根目录的 `AGENTS.md`。
