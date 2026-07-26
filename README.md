# ddup

面向 A 股市场的日频量化策略回测引擎。因子以声明式 YAML 定义，策略以 Python 子类编写，行情数据库以表单映射接入。引擎提供撮合、费用、滑点、风控、统计及全链路前视屏蔽，研究与回测共享同一因子计算路径。

> **重要声明：** 本软件是日频量化策略研究工具，不连接任何券商接口、不执行真实交易、不构成投资建议。引擎输出的买卖名单与持仓目标是策略代码的计算结果——将其转化为实盘订单的责任、合规义务和交易风险完全由使用者自行承担。历史回测表现不代表未来收益，回测中无法模拟日内价格路径、流动性冲击、市场制度变化等因素，实盘结果可能与回测存在显著差异。在中国境内用于辅助实盘交易的使用者须自行遵守《证券市场程序化交易管理规定（试行）》及相关法律法规。作者与贡献者不为因使用本软件而产生的任何交易损失负责。

## 快速开始

阅读文档 [docs/user_guide.md](docs/user_guide.md)，完成数据库路径和字段名适配

```bash
# 最简示例：裸因子轮动
python scripts/run.py strategies/examples/simple_rotation.yaml \
    --start 20240101 --end 20240630

# 从 Python 调用的完整示例
from adapters.tushare import TushareBackend
from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import load_strategy

strategy = load_strategy("strategies/examples/topk_momentum.yaml")
provider = DataProvider(TushareBackend("/path/to/market.db"))
engine = Engine(strategy, provider)
result = engine.run("20240101", "20240630")
print(result["statistics"])
```

## 示例策略

从简单到复杂，每个示例专注展示一组特性：

| 示例 | 特性 | 适合 |
|---|---|---|
| `simple_rotation` | 裸因子轮动 + 止损条件单，50 行 | 入门 |
| `topk_momentum` | + `on_fills` 成交感知、`buy_weights` 加权分配、动态止损 | 进阶 |
| `target_allocator` | `target_value` 目标仓位、`risk_rules` 风控、`schedule` 周频调仓 | 仓位管理 |
| `condition_hunter` | `buy_conditions` 条件买入、自定义 handler 注册 | 条件单 |
| `state_machine` | 三钩子全开、市场状态机、多模型投票、自定义因子库 | 架构 |

## 能力一览

| 能力 | 怎么用 |
|---|---|
| 对接数据库 | `adapters/` 下填一张 Python dict：`"open": "daily.open"` |
| 定义因子 | `factors/library.yaml` 里一行 `expr: "roc(close_hfq, 20)"` |
| 自定义因子库 | 策略 YAML 中 `factor_library: my_factors.yaml` |
| 策略买卖 | 继承 `Strategy`，实现 `select()` 返回 buy/sell 名单 |
| 目标仓位 | `select()` 返回 `target_value: {symbol: 市值}` |
| 加权买入 / 部分卖出 | `select()` 返回 `buy_weights` / `sell_shares` |
| 条件单 | YAML 声明 `conditions: {stop_loss_pct: 0.08}` |
| 条件买入 | `select()` 返回 `buy_conditions: [{type: "LIMIT_BUY", ...}]` |
| 自定义条件单 | `register_condition_handler("TYPE", handler)` |
| 成交感知 | 实现 `on_fills(trades, provider)` 钩子 |
| 动态调参 | `calc_conditions` 中按 `holding_days` 调整条件单参数 |
| 组合风控 | YAML 声明 `risk_rules: {max_drawdown: 0.15}` |
| 调仓频率 | YAML 声明 `schedule: {frequency: weekly}` |
| 自定义滑点/费率 | YAML `config` 键覆盖默认值 |
| 列裁剪 | 策略声明 `REQUIRED_FIELDS`，引擎自动按需取列 |
| 因子研究 | `compute_factor("mom20", df)` → IC 评估 |
| 绩效归因 | `brinson_attribute("result.db")` |

## 文档

- [用户指南](docs/user_guide.md) — 设计哲学、架构、能力全景
- [数据后端对接](docs/backend_guide.md) — 填表法、契约口径、能力空位
- [因子库指南](docs/factor_library.md) — DAG 模型、19 个算子、伪列、研究陷阱
- [策略系统指南](docs/strategy_guide.md) — YAML 结构、select 协议、条件单、风控、CLI

## 开发

```bash
pytest tests/ -v                              # 全部测试（仅 fixtures）
pytest tests/ --cov=btcore -v                  # 覆盖率
ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/   # Lint
python scripts/check_anticorrupt.py            # 反破坏检查
```

## 许可

[MIT](LICENSE)
