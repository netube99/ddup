# 开发与文档维护

[文档入口](index.md) · [架构](../ARCHITECTURE.md) · [审查协议](review_protocol.md)

## 开始与验收

先读项目 AGENTS、相关目录的 AGENTS、最近审查记录及 backlog，记录当前 commit/diff。
普通修改只检查 diff 和直接调用链；完整审查需要明确范围。不要覆盖用户已有修改。
环境与首次运行见 [README](../README.md)，本地配置与私有资产见 [目录归属](repository_layout.md)。

```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/
uv run python scripts/check_anticorrupt.py
uv run python scripts/check_skill_sync.py
# 门禁：检查未决阻塞，运行两项机械检查与完整测试；Ruff 仍需单独运行
uv run python scripts/review_gate.py done --full
```

测试使用已提交 parquet fixtures，不需要本机行情库；ML 可选依赖影响相关测试是否跳过。
覆盖率可用 pytest tests/ --cov=btcore -v；真实数据 dump_fixtures.py 会更新 fixtures，
只在明确需要时执行。测试数量随实现变化，以当次输出为准。

计算修复先建立独立期望值的失败用例，再最小修复。涉及资金、撮合、公司行为、统计或
时序时核验相应不变量和端到端结果；前视相关改动加未来扰动/截断测试。
[核查矩阵](audit_checklist.md) 用于选择证据，不要求每个修改都全仓重审。

## 单一事实源

| 内容 | 维护位置 |
| --- | --- |
| 项目定位、首次运行 | README |
| agent 全局约束、禁止设计 | AGENTS |
| 依赖方向、生命周期、跨模块契约 | ARCHITECTURE |
| 接口/参数/算子/数据语义 | 对应 docs 专题指南 |
| 研究判断与实验方法 | factor_research、strategy_patterns、results_analysis |
| 任务触发、步骤、停止条件、按需阅读 | .agents/skills/*/SKILL.md |
| 已发生的发现、证据与处置 | review_backlog.yaml、reviews/YYYY-MM-DD |

接口变动同批更新代码、对应指南和受影响 skill 链接/流程。check_skill_sync 对账
算子、select/filter/conditions、引擎默认值、ML 契约、CLI 参数及本地链接；
它不能证明文档语义全部正确。不要为通过检查删除事实覆盖。

## 文档和 skill 写作约定

- 面向任务写结论、规则、例子与限制；设计解释保留“为什么”和实现入口。
- 完整列表只维护一份，其余位置链接；用稳定的显式锚点，避免行号和“见第 N 节”。
- 长文开头给按需入口；新增文档接入 index，新增机制接入架构设计地图。
- 区分当前支持行为、已证实局限、研究假设和历史样本；不把实验输赢升级成普遍规则。
- 示例注明必需输入、路径解析方式和写入副作用；不得用假文件暗示可直接运行。
- skills 的 name/description 明确任务触发，正文保留关键决策、易错点、输出和停止条件。
  详细 API 链到专题文档，不重复手册，也不强迫无关任务加载完整研究流程。
- skill 不自行扩大用户授权、强迫持续实验或要求修复非阻塞事项。
- 历史材料保留 ID、独立期望、原始数值、复现命令和限制；删除讨论流水账，
  不删除仍在支撑 backlog 的证据。私有研究结论不搬进可分发文档。

完成文档变更后检查相邻 AGENTS、链接、代码示例、CLI --help 和同步检查。
涉及检查脚本时运行相应测试。验收报告只记范围、结果与未验证项，不写逐步开发流水账。
