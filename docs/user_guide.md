# ddup 用户指南

面向 A 股市场的日频量化策略回测引擎。因子以声明式 YAML 定义，策略以 Python 子类编写，行情数据库以表单映射接入。引擎提供撮合、费用、滑点、风控、统计及全链路前视屏蔽，研究与回测共享同一因子计算路径。

---

## 设计原则

### 五层架构

```
btcore/          — 引擎内核
adapters/        — 数据后端
factors/         — 因子定义
strategies/      — 策略
research/        — 研究工具
```

依赖单向：`strategies → btcore`、`factors → btcore.factors`、`research → btcore`。
下层不导入上层，零循环依赖。

### 机制与配置分离

`btcore/` 只提供机制——数据加载、撮合、成本、风控、统计、因子物化、策略加载。
不含任何因子公式或买卖规则。

用户代码（`adapters/`、`factors/library.yaml`、`strategies/`）只声明内容——
数据位置、因子表达式、买卖逻辑。两层通过接口契约解耦，引擎升级不影响已有策略。

### 声明式优先

因子公式、条件单、风控阈值、调仓频率、数据位置均在 YAML 或配置 dict 中声明。
仅买卖逻辑需以 Python 编写——这是策略的实质，无法声明化。

### 鸭子类型能力开关

辅助能力（ST 过滤、行业分类、指数成分、基准对比）以后端对象上对应方法的有无
作为开关。表单中填写了对应空位，方法即自动装配；未填写则能力关闭。
对接成本与需求精确匹配。

### 软回退与 fail-fast 的分界

- **可选能力缺失**（ST 表、行业表、指数成分表）：告警一次，对应规则不生效，策略继续运行。
- **明确声明的依赖缺失**（因子引用的伪列无后端支持、必需列缺失、因子名不存在）：加载或 preload 阶段直接报错，不产生静默错误结果。

### 前视屏蔽

- 因子在 preload 阶段一次性物化为因果列（滚动窗口与截面聚合仅用当日及之前数据）
- 数据访问通过当前模拟日钳制
- T 日信号 T+1 撮合，条件单 T 日声明 T+1 盘中触发

---

## 能力全景

| 能力 | 配置位置 | 文档 |
|---|---|---|
| 数据后端对接 | `adapters/` 填表或实现 DataBackend | [backend_guide.md](backend_guide.md) |
| 因子定义 | `factors/library.yaml` | [factor_library.md](factor_library.md) |
| 策略逻辑 | `strategies/` 继承 Strategy | [strategy_guide.md](strategy_guide.md) |
| 股票过滤 | YAML `filter_rules` | [strategy_guide.md](strategy_guide.md) |
| 条件单 | YAML `conditions` + ConditionBuilder | [strategy_guide.md](strategy_guide.md) |
| 组合风控 | YAML `risk_rules` | [strategy_guide.md](strategy_guide.md) |
| 调仓频率 | YAML `schedule` | [strategy_guide.md](strategy_guide.md) |
| 费率与滑点 | YAML `config` 键 | [strategy_guide.md](strategy_guide.md) |
| 成交价模式 | YAML `config.execution_price` | [strategy_guide.md](strategy_guide.md) |
| 条件买入 | select 返回 `buy_conditions` | [strategy_guide.md](strategy_guide.md) |
| 目标仓位调仓 | select 返回 `target_value` | [strategy_guide.md](strategy_guide.md) |
| 因子研究 | `research/factor_eval.py` | [factor_library.md](factor_library.md) |
| 绩效归因 | `research/attribution.py` | [strategy_guide.md](strategy_guide.md) |
| CLI 回测 | `scripts/run.py` | [strategy_guide.md](strategy_guide.md) |
| HTML 报告 | `scripts/report.py` / `research/report.py` | [strategy_guide.md](strategy_guide.md) |
| 多 run 对比 | `scripts/compare.py` | [strategy_guide.md](strategy_guide.md) |
| 程序式 API | `Engine(strategy, provider).run()` | [strategy_guide.md](strategy_guide.md) |

---

## 工作流

**对接数据**（一次性）：在 `adapters/` 下新建文件，填写表单，初始化校验通过即可。
详见 [数据后端对接指南](backend_guide.md)。

**定义因子**：在 `factors/library.yaml` 中登记公式，研究侧验证 IC 后引入策略。
详见 [因子库指南](factor_library.md)。

**编写策略**：在 `strategies/` 下创建 YAML 与 Python 文件，先以基础买卖名单验证逻辑，
再叠加条件单、风控、调度。详见 [策略系统指南](strategy_guide.md)。

**回测迭代**：CLI 快速验证，Python API 精细分析。结果库支持多轮累积，便于参数扫描。

---

## 文档索引

- [数据后端对接指南](backend_guide.md) — 填表法、契约口径、能力空位
- [因子库指南](factor_library.md) — DAG 模型、算子表、伪列、研究陷阱
- [策略系统指南](strategy_guide.md) — YAML 结构、返回协议、条件单、风控、CLI
