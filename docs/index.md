# ddup 文档导航

从任务进入，只读所需主题。接口规则以专题指南为准；架构解释跨模块不变式；
skill 提供操作流程；历史记录只证明其标注版本和样本。

## 按任务阅读

| 任务 | 起点 | 深入 |
| --- | --- | --- |
| 首次运行 | [README](../README.md) | [回测 CLI](cli_and_research.md#run) |
| 理解系统 | [架构](../ARCHITECTURE.md) | [目录归属](repository_layout.md) |
| 接入行情、财报、行业与指数 | [后端指南](backend_guide.md) | [公告日对齐与数据限制](backend_guide.md#alignment) |
| 写因子、查算子、构建 DAG | [因子机制](factor_library.md) | [物化与计算域](factor_library.md#materialization) |
| 评估因子、多因子合成 | [因子研究](factor_research.md) | [评估命令](cli_and_research.md#factor-eval) |
| 设计交易规则 | [五要素与策略模式](strategy_patterns.md) | [钩子和 YAML 参考](strategy_guide.md) |
| 配仓、条件买卖、自定义退出 | [select 协议](strategy_guide.md#select) | [条件单](strategy_guide.md#conditions)、[执行口径](strategy_guide.md#execution) |
| 训练/使用 ML 模型 | [ML 指南](ml_guide.md) | [训练](ml_guide.md#training)、[观测](ml_guide.md#observability) |
| 回测、扫描、报告、回放 | [CLI 参考](cli_and_research.md) | [实验方法](factor_research.md#experiments) |
| 解释收益和交易 | [结果分析](results_analysis.md) | [统计](results_analysis.md#statistics)、[Brinson](results_analysis.md#attribution) |
| 实盛建账、对账、预览与执行 | [实盘指南](live_guide.md) | [每日闭环](live_guide.md#workflow)、[排错](live_guide.md#troubleshooting)、[离线演练](live_guide.md#verification) |
| 实盘 V2 重制与验收 | [重制方案](live_redesign.md) | 账户事实、执行计划、实施所有权与 A01–A24 验收矩阵 |
| 修改代码与维护文档 | [开发指南](development.md) | [AGENTS](../AGENTS.md)、[审查协议](review_protocol.md) |
| 有边界审查/独立核算 | [核查矩阵](audit_checklist.md) | [backlog](review_backlog.yaml)、[历史证据](reviews/README.md) |

## 设计信息地图

| 设计问题 | 权威位置 |
| --- | --- |
| 一天内的执行顺序、首日信号、整日缺行情 | [引擎生命周期](../ARCHITECTURE.md#lifecycle) |
| 裸价/后复权、T+1、前视、软回退和 fail-fast | [跨模块契约](../ARCHITECTURE.md#contracts) |
| 21 算子、布尔运算、where、warmup | [因子表达式与算子](factor_library.md#expressions) |
| CSE、全市场广度、交易域/计算域 | [因子物化](factor_library.md#materialization) |
| 普通单/条件单、滑点、涨跌停、量 cap、零股 | [撮合执行](strategy_guide.md#execution) |
| 财报 PIT、静态行业、新股名单、指数快照边界 | [数据对齐](backend_guide.md#alignment) |
| panel/holding、meta v3、同源特征与模型解释权 | [ML 契约](ml_guide.md#configuration) |
| 多 run、事件语义、费用与归因残差 | [结果库与分析](results_analysis.md) |
| 账本唯一来源、衍生表重建与回测复用 | [实盘账本](live_guide.md) |

<a id="skills"></a>
## Agent 任务入口

| skill | 何时加载 |
| --- | --- |
| [ddup-research-loop](../.agents/skills/ddup-research-loop/SKILL.md) | 多轮研究、恢复研究状态、决定下一轮或收官 |
| [ddup-factor-research](../.agents/skills/ddup-factor-research/SKILL.md) | 因子设计、IC/分层/衰减、合成、广度 |
| [ddup-strategy-craft](../.agents/skills/ddup-strategy-craft/SKILL.md) | 编写/修改策略、仓位和条件单 |
| [ddup-experiment-design](../.agents/skills/ddup-experiment-design/SKILL.md) | 对照实验、参数扫描、样本外验证 |
| [ddup-backtest-analysis](../.agents/skills/ddup-backtest-analysis/SKILL.md) | 解释回测、独立核算、定位异常交易 |
| [ddup-ml-research](../.agents/skills/ddup-ml-research/SKILL.md) | 模型训练、评估及策略消费 |
| [ddup-live-ops](../.agents/skills/ddup-live-ops/SKILL.md) | 已授权的实盘账本操作与排错 |
| [ddup-review-gate](../.agents/skills/ddup-review-gate/SKILL.md) | 代码审查、发现登记与修复验收 |

普通开发不必加载研究流程；单一专题任务不必遍读所有 skills。操作流程引用上面的
专题文档，避免维护两份算子表、参数表或协议表。
