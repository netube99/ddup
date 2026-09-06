# 开发收敛协议（审查门禁）

目标：让审查循环收敛——每轮审查要么修复真问题（P0/P1），要么显式记录决策，
不存在"反复查出无穷问题"的状态。机械执行由 `scripts/review_gate.py` 承担，
本文档是 AGENTS.md「开发收敛协议」一节的展开。

## 为什么需要门禁

审查是把代码与一个隐式理想比较，这个理想无界（正确性、性能、风格、可扩展性、
文档……），任何有限代码库都不可能在所有轴上最优——**"零发现"不是可达状态**。
用"零发现"当审查门禁，循环必然发散。

收敛的做法：

1. 发现强制分级，只有阻塞级（P0/P1）决定本轮是否结束；
2. 非阻塞级（P2/P3）自动登记 backlog，不要求本轮修复（边际修复的期望值为负：
   引入回归的风险 > 边缘收益）；
3. 用"连续两轮无新增"判据显式终止审查；
4. "可用"由 done-bar 定义（可测试、可判定），不是"审查零发现"。

## 发现分级

| 级别 | 定义 | 处置 |
|---|---|---|
| P0 | 数据损坏或错误结果（收益/仓位/现金错误、崩溃） | 阻塞：修复后 `close`，或用户显式 `close --waive` |
| P1 | 契约违背：静默错误行为（与已写契约/文档不符但不报错） | 阻塞：同上 |
| P2 | 已知局限 / 潜伏缺陷（特定输入才坏，当前路径不可达） | 非阻塞：登记 backlog |
| P3 | 文档漂移 / 观感 / 观察项 | 非阻塞：登记 backlog |

阻塞项只有两个出口：修复后 `close <ID>`，或用户决策 `close <ID> --waive <原因>`。
其余出路不存在——未决 P0/P1 会让门禁持续 FAIL，直到被处理。

## findings 文件格式

任何审查（full/diff、agent 或人工）的产出必须是以下 YAML，交 `review_gate.py check`
验收。**每条发现必须带 rule、file、line、note 四要素**；缺任一 → 门禁拒绝
（exit 2，fail-fast，不产生任何结论）——没有证据的"问题"不计数。

```yaml
meta:
  date: 2026-08-06        # YYYY-MM-DD，必填
findings:
  - id: F-EMA-01          # 区段-编号（如 F-EMA-01、E-CFG-01），必填
    severity: P1          # P0/P1/P2/P3 之一，必填
    rule: "docs/factor_library.md §6.4: ema_bullish 应为 0-3 分"  # 被违反的已写规则，必填
    file: btcore/factors/ops.py    # 相对仓库根，必填，须真实存在
    line: 42              # 正整数，必填
    note: "bool 加法被 OR 化，列值仅 0/1；float(eb)<3 恒真"       # 现象与证据，必填
```

## 门禁流程

1. 审查产出 findings YAML（文件内 id 不得重复）。
2. `python scripts/review_gate.py check findings.yaml --scope full|diff`
   - exit 1：存在未决 P0/P1（本轮新增 + 历史未决都会列出）→ 本轮不结束；
   - exit 0：门禁通过，P2/P3 已登记 `docs/review_backlog.yaml`，打印收敛判据；
   - exit 2：findings 不合法 → 修正后重跑。
3. 处置阻塞项：修复 → `close <ID>`；用户决定不修 → `close <ID> --waive "<原因>"`。
4. 重跑 check，直到门禁通过；收敛判据触发后该范围审查终止。
5. 进入 done-bar：`python scripts/review_gate.py done [--full]`。

重复上报语义：已在 open 的 id 再上报 → 跳过不计数；已 closed 的 id 再上报且
内容未变 → 跳过（防重复修）；已 closed 的 id 内容变化 → 视为复发，重新 open。

## 范围与冷却

- `--scope full`：全量审查（130 项 checklist / 影子核验 / 子系统审计等）。
  同范围间隔 <14 天 → WARN 并记录时间；每子系统每里程碑至多一次 full。
- `--scope diff`：只审改动行（diff 范围），默认值。
- 距上次 full 超 60 天 → HINT 提示可安排一次全量审查。

## backlog 管理

`docs/review_backlog.yaml` 由脚本自动维护，勿手改结构：
- `open`：未决发现（P2/P3 + 未决 P0/P1——后者是门禁 FAIL 的来源）；
- `closed`：已修复（`resolved`）或已放弃（`waived` + 原因）；
- 查看：`python scripts/review_gate.py backlog [--closed]`。

## done-bar：可用的定义

"可用" = 以下全部成立，**不是"审查零发现"**：

1. `python scripts/check_anticorrupt.py` 通过（反破坏 linter）；
2. `python scripts/check_skill_sync.py` 通过（skill 与代码事实同步）；
3. `pytest tests/ -q` 全绿；
4. 最新回测 `cross_validate.py` 无 P0/P1 问题项；
5. 实盘 `live.py sync` 对账一致。

`review_gate.py done` 自动跑 1-2（`--full` 加 3），4-5 需真实库手动验收。
done-bar 全绿即"可用"——此后每次迭代只做 diff-scoped 审查 + done-bar，不再全量翻。

## 与既有审计流程的关系

全量审计照常进行（`docs/audit_checklist.md`、影子核验、子系统审计），但：

- 审计记录文档（`docs/audit_record_*.md`）不是终点，产出必须转成 findings YAML 过门禁；
- 审计中的 P2/P3 一律登记 backlog，不在本轮修复；
- 每子系统每里程碑至多一次全量审计，之后只做 diff-scoped 审查。
