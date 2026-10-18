# ddup

A 股个股日频量化研究与回测引擎。用 YAML 定义因子，用 Python 策略表达交易意图，
引擎处理因果物化、T+1 撮合、公司行为、费用与多 run 结果记录。

[文档导航](docs/index.md) · [架构与设计契约](ARCHITECTURE.md) · [开发指南](docs/development.md)

## 开始使用

需要 Python 3.12+ 和 uv。从仓库根目录执行：

```bash
uv sync --extra dev
uv run pytest tests/ -q
```

测试使用仓库 fixtures，不需要真实行情库。实际回测另需配置后端与因子库；
本机配置不随仓库分发，已有文件请直接编辑，不要覆盖：

```bash
cp -n adapters/tushare.py.template adapters/tushare.py
cp -n factors/library.yaml.template factors/library.yaml
```

按[后端指南](docs/backend_guide.md)填写数据库路径和表单；也可实现自己的 DataBackend。
默认 CLI 后端为 `adapters.tushare:TushareBackend`，通过 `DDUP_BACKEND=module:Class`
切换。支持与 [tushare_db](https://github.com/netube99/tushare_db) 管理的行情库配合使用。

```bash
uv run python scripts/run.py strategies/examples/bare_bones/config.yaml \
  --start 20240603 --end 20240628 --out results/first.duckdb --no-report
uv run python scripts/cross_validate.py results/first.duckdb --run-id 1
uv run python scripts/report.py results/first.duckdb --run-id 1 --out results/first.html
```

`run.py` 不传 `--out` 只使用内存库；默认另生成 HTML 报告。
示例展示机制，不代表通过收益验证的策略。

## 能做什么

| 工作 | 入口 |
| --- | --- |
| DuckDB 填表接入、扩展列、公告日对齐 | [数据后端](docs/backend_guide.md) |
| 表达式算子、DAG、全市场广度、CSE | [因子定义与物化](docs/factor_library.md) |
| IC、分层、衰减、滚动 IC/ICIR 合成 | [因子研究](docs/factor_research.md) |
| 名单、目标市值、条件买卖、自管理调仓 | [策略接口](docs/strategy_guide.md)与[示例模式](docs/strategy_patterns.md) |
| panel / holding 模型训练与 ONNX 分数消费 | [ML](docs/ml_guide.md) |
| 扫描、绩效、交易回放、Brinson 归因 | [命令](docs/cli_and_research.md)与[结果分析](docs/results_analysis.md) |
| 成交账本、对账、次日操作单 | [实盘账本](docs/live_guide.md) |
| Agent 按任务执行研究与维护 | [Skill 路由](docs/index.md#skills) |

软件用于研究和历史回测；历史收益不代表未来表现。账本与操作单功能不连接券商自动下单。
