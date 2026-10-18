# 项目文件整理（2026-10-18）

## 范围与基线

- 用户任务：分析项目文件归属，整理历史材料，删除失效、多余与临时内容。
- 起始 commit：`67abff3dc22fcf0bee80650acc236c41f0fddc97`，工作区干净。
- 已查阅 2026-10-12 审查基线与 backlog：未决 P0/P1 为 0，P2 为 25、P3 为 29。
- 范围：目录与引用、忽略规则、可再生缓存及空目录；不开展全量计算审查。
- 完成条件：历史文件完整迁移、引用可达、证据脚本能执行、受保护资产内容一致，
  Ruff、反破坏、skill 同步与全量 pytest 通过。

## 处置

- 11 份历史审查记录/findings/JSON 迁至 `docs/reviews/YYYY-MM-DD/`，按日期索引。
- 1 份 Python 证据脚本迁至 `scripts/diagnostics/`，修正项目根路径定位。
- 同步文档、AGENTS、skill 与 backlog 中的路径；历史数值、发现等级和处置状态不变。
- 增加 `docs/repository_layout.md`，说明源码、命令、测试、文档、私有资产与产物的归属。
- 整理 `.gitignore`：合并同类规则，去掉被 `*.db` 覆盖的 `market.db`、无对应忽略规则的
  fixtures 反向例外，以及没有站点配置/构建依赖的 VitePress 产物规则；补齐 Ruff 与覆盖率输出。
- 清理本项目可再生缓存、覆盖率文件及空目录；排除 `.venv/`、`.git/`。
  共删除 185 个生成文件、34 个缓存目录，释放 2,941,519 字节（约 2.8 MiB）；
  删除 6 个空目录：`.pi/tasks/session-48440-48440`、`.pi/tasks`、`.pi`、
  `archive/results`、`archive`、`data`。
- 未发现可凭现有证据删除的业务源码或回归测试。保留研究辅助 CLI、回归用例、模板、
  fixtures、私有策略/模型/报告、研究状态、开发笔记、日志与实盘文件。
  模型内容重复对应不同配置消费，操作单/报告重复也保留各自的业务身份。

## 验收

| 检查 | 实际结果 |
| --- | --- |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests/ -q -ra -p no:cacheprovider` | 856 passed，0 failed / 0 skipped，73.72 秒 |
| `.venv/bin/ruff check --no-cache btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/` | All checks passed，包含迁移后的证据脚本 |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/review_gate.py done` | 未决阻塞为 0；反破坏与 skill 同步通过；pytest 已单独运行 |
| `PYTHONDONTWRITEBYTECODE=1 .venv/bin/python scripts/diagnostics/review_evidence_20261012.py` | 合成场景：越界买单拒绝；100 股现金分红期望/实际均为 40，差值 0；三日完整结算，无陈旧卖单 |
| 文件完整性核对 | 441 个源码、测试、策略、模型、报告、研究状态、笔记、日志及实盘文件 SHA-256 一致 |
| 历史材料与 backlog | 12 份迁移文件与原版本比较，仅引用路径/脚本根定位变化；backlog 仅证据路径变化，无 ID/等级/状态改写 |
| 路径与导航 | 56 个本地 Markdown 链接目标存在；旧完整路径引用清零 |
| `git diff --check` | 通过 |

合成场景中的涨停拒单、缺行情及分红价格锚点告警为原有边界场景的预期输出，没有静音。
没有新增阻塞项或重启全量审查。结构整理完成后停止检查循环。

## 范围限制

本次没有计算逻辑变更；不重跑真实行情窗口或模型训练，不修改研究基线与实盘账本。
实际券商对账未验证。既有非阻塞 backlog 54 项留存，本次文件整理不处理它们。
