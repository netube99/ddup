# AGENTS.md — ddup

A 股个股日频回测引擎，Python 3.12+、uv、hatchling。
先按任务查 [文档导航](docs/index.md)，仅加载相关指南和 skill。
本文只保留全局工作约束；功能细节的主入口是 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 文件与范围

- 用户扩展在 `adapters/`、顶层 `factors/`、`strategies/`；机制在 `btcore/`。
  `research/` 是可导入工具库，`scripts/` 是 CLI，不能按名字假定 research 全部为纯函数。
- 修改前读最近[验收记录](docs/reviews/README.md)及 `docs/review_backlog.yaml`，记录
  commit、已有 diff、涉及路径、验收命令和完成条件。保留用户已有改动。
- 普通修改只检查 diff 及直接调用链，不自动开启全仓审查、策略研究或优化。
- 文件新增/迁移遵循 [目录归属](docs/repository_layout.md)；编辑后检查同目录 AGENTS。
- 本机后端、因子库、私有策略/模型、研究状态、结果库、实盘账本不是缓存。
  未经用户明确授权，不写真实账本、不改研究基线或线上策略；核验在副本执行。

## 必守设计契约

完整语义与边界见[架构契约](ARCHITECTURE.md#contracts)。

- 日期 `YYYYMMDD` 字符串；面板 `MultiIndex(trade_date, symbol)`。
- 裸价用于撮合/成本/估值，后复权 `*_hfq = 裸价 × adj_factor` 用于价格因子/排名。
- T 日信号 T+1 撮合；买入当天锁仓，不能当天条件卖出。首日用前一交易日播种。
- 策略通过 provider 查询历史；不得绕过锚点直查未来数据。财报由数据层按公告可得日
  对齐到日频网格；引擎不推断季度、不替后端消除数据自身的前视。
- 可选能力缺失告警软回退；声明的必需列、因子、表单引用、模型契约缺失应 fail-fast。
- 加载池整日无 bar 时，公司行为、结算、持仓交易日计数与解锁照常；清空单日订单和
  持仓条件单，不调用决策钩子。整窗无行情报错。

## 架构依赖与禁止重引入

`scripts/check_anticorrupt.py` 强制检查；边界说明见[架构分层](ARCHITECTURE.md#layers)。

- `types.py` / `constants.py` 不依赖其他业务模块。
- `match/manual.py`、`match/conditions.py` 互不 import，仅共享 `match/core.py`。
- `stats.py` 是纯函数，不 import duckdb/sqlite3，不依赖 provider/engine。
- `btcore/factors/*` 不依赖 engine/match/database/provider。
- `engine.py` 不被 btcore 内部模块 import；btcore 不 import 用户策略、顶层 factors、adapters。
- 顶层 factors 仅可依赖 btcore.factors；全局无循环 import。
- 不恢复 `factors/builtin.py` 或具体因子内置机制；因子定义为纯 YAML，禁止旧
  Factor/CrossSection/ExprFactor/FunctionFactor/StrategyAdapter/FactorSpecItem/
  FactorPipeline/equal_weight_percentile/register_factor 类层次或注册模式。
- 算子 `_OPS` 是固定表；条件单 handler 的进程级注册是另一机制。
- Strategy ABC 仅声明数据属性和钩子，不加 take_profit_mode/trailing_conservative 等行为开关。
- 不恢复 Holding.last_adj_factor、启发式公司行为补偿、GuardedProvider 包装器。
- 模型经 YAML `models` 声明，scope 由特征决定；策略不得自行加载 ONNX。
  不恢复 MLConfig/MLEvaluator/config['_ml_config']/type: ml_feature/硬编码模型意图。

## 开发与验收

完整流程、分级和门禁语义以 [review_protocol.md](docs/review_protocol.md) 为准。

- P0：已复现的广泛数据损坏或核心计算错误。P1：支持路径上的错误计算、前视、
  错误成交或契约违背。两者阻塞；低频或旧 backlog 等级不能作为降级理由。
- P2：支持路径不可达或明确超范围的已证实局限。P3：文档漂移/有依据的维护建议。
  默认登记、不自动修；用户明确指定的文档、重构等任务按授权范围执行。
- 审查默认交付证据，不自动修复；修复默认仅处理已确认 P0/P1。未复现推测标待验证，
  不据此改代码。findings 需 `id/severity/rule/file/line/note`，包含触发、可运行复现、
  实际值、独立期望及依据、影响对象；复用历史 ID，不因措辞/行号变化重开。
- 计算修复先写能暴露旧错误的独立回归，再最小改动；资金/撮合/公司行为/统计/时序
  修改需核验直接不变量与端到端结果。因果修改加未来扰动或截断检查；数值口径变化
  说明旧结果是否需重跑。不可静音、改阈值/断言、降级或伪造用户 waive 换取通过。
- 范围完成且无未决 P0/P1 即停止。P2/P3 不延长循环，不要求两轮无新增。
  同一问题两次修复仍失败，停止自动改动并交付复现、原因及方案。
- 无新 diff、失败测试、新复现或用户扩大范围，不再重审。full 审查还受 14 天冷却限制；
  修复验收用 diff。CONVERGED 仅表示登记项无阻塞，不代替覆盖和计算证据。

在项目环境执行，详细命令见[开发指南](docs/development.md)：

```bash
python scripts/review_gate.py done --full
ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/
```

`done --full` 检查未决阻塞，运行反破坏、skill 同步与完整 pytest；Ruff 另跑。
最终分别报告：范围、阻塞项、非阻塞待办、实际检查 passed/skipped/failed、未验证范围。
自动检查、独立计算抽样、真实券商对账是三个层次，不能互相替代。

## 文档与代码风格

- Ruff E/F/I/N/W，100 字符行宽、双引号、类型标注；代码与注释不使用 emoji。
- 设计写 docs，docstring 最小化。导航、接口参考、执行 skill 各司其职，不复制整套接口表。
- CLI、算子、YAML、select、config 默认值或 ML meta 契约变化，同批更新对应 docs/skills，
  跑 `python scripts/check_skill_sync.py`。文档使用符号定位，避免维护易漂移的源码行号/数量。
- 策略实现前写清信号周期、入场过滤与触发、离场优先级、仓位、边界处理，见
  [策略五要素](docs/strategy_patterns.md#spec)。空 calc_conditions 不代表无退出：先看 sell/target_value。
