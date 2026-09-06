---
name: ddup-review-gate
description: 审查收敛门禁：审查/审计产出的分级、验收与终止判据。审查或审计完成后产出 findings YAML 并用它收敛——P0/P1 清零或 waive、P2/P3 入 backlog、收敛判据触发即停。触发场景：审查代码、审计子系统、review 发现清单、验收 audit_record 产出。
---

# ddup-review-gate — 审查收敛门禁

审查发现必须分级并过门禁（`scripts/review_gate.py`）。禁止以"零发现"作为审查
目标——该状态不存在，用它做门禁只会让审查循环发散。完整协议见
`docs/review_protocol.md`。

## 分级

| 级别 | 定义 | 处置 |
|---|---|---|
| P0 | 数据损坏 / 错误结果 | 阻塞：修复后 close，或用户 waive |
| P1 | 契约违背（静默错误行为） | 阻塞：同上 |
| P2 | 已知局限 / 潜伏缺陷 | 登记 backlog，本轮不修 |
| P3 | 文档漂移 / 观察项 | 登记 backlog，本轮不修 |

## 流程

1. 审查产出 findings YAML（模板见下）。每条必须带 `rule`（被违反的已写规则）、
   `file`（相对仓库根）、`line`、`note`——缺任一即无效发现，门禁拒绝。
2. 验收：

   ```bash
   python scripts/review_gate.py check <findings.yaml> --scope full
   ```

   - exit 1：有未决 P0/P1（含历史未决）→ 修复后 `close <ID>`，或用户决策
     `close <ID> --waive "原因"`，重跑 check；
   - exit 0：P2/P3 已入 `docs/review_backlog.yaml`，看收敛判据；
   - exit 2：findings 不合法，修正后重跑。

3. 收敛判据：一轮 check 无 P0/P1 且无新增发现（CONVERGED）→ 该范围审查终止，
   之后只做 diff-scoped 审查（`--scope diff`，只审改动行）。
4. 可用性验收：`python scripts/review_gate.py done [--full]`——done-bar =
   反破坏 linter + skill 同步 + 测试全绿 + cross_validate 干净 + live sync 一致。

## findings 模板

```yaml
meta:
  date: 2026-08-06
findings:
  - id: F-EMA-01
    severity: P1
    rule: "docs/factor_library.md §6.4: ema_bullish 应为 0-3 分"
    file: btcore/factors/ops.py
    line: 42
    note: "bool 加法被 OR 化，列值仅 0/1；float(eb)<3 恒真"
```

## 铁律

- 无效发现（缺 rule/file/line/note 任一、id 格式非法、file 不存在、severity 非
  P0-P3）→ 门禁拒绝，不计数、不产生结论
- 未决 P0/P1 使门禁持续 FAIL，唯二出口：`close`（修复）或 `close --waive`（用户决策）
- full 审查间隔 ≥14 天（`check --scope full` 自动冷却检查）；间隔内只允许 diff
- 已 closed 的 id 再上报且内容未变 → 跳过；内容变化 → 复发，重新 open
- "可用" = done-bar 全绿，不是"审查零发现"
