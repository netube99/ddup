---
name: ddup-review-gate
description: 审查收敛门禁：有边界审查、独立证据与分级、P0/P1 验收、P2/P3 backlog、三层 done-bar 与强制停止条件。审查或审计代码、产出 findings 和验收 audit_record 时使用。
---

# ddup-review-gate — 维护期审查门禁

先读 AGENTS.md「开发收敛协议」、最近审查记录与 backlog。完整流程见
`docs/review_protocol.md`；基线见 `docs/review_record_20261012.md`。

## 范围与证据

- 开始前锁定 commit/diff、使用路径、覆盖范围、验收命令、停止条件。
- 普通任务只审 diff 及直接影响的调用链。全量须有用户要求或共用计算链路失效证据。
- 每条发现必须含触发条件、可运行复现、实际值、独立期望值及依据、影响对象。
  未复现猜测写“待验证”，不作缺陷、不阻塞、不据此改代码。
- P0：已复现的广泛数据损坏或核心计算错误。P1：支持路径上的错误计算、前视、
  错误成交或契约违背。特定输入、低频、旧项为 P2 不能作为降级理由。
- P2：当前支持路径不可达或明确超出支持范围的已证实局限。P3：文档/维护建议。
  P2/P3 只登记；未经用户明确指定，不修复、不延长审查。
- 审查请求默认产出证据与结论；修复请求默认只修确认的 P0/P1，先建立独立回归。
  不能以静音、放宽阈值、改断言、降级或伪造 waive 换取通过。

## findings 与流程

```yaml
meta:
  date: 2026-10-12
findings:
  - id: F-EXAMPLE-01
    severity: P1
    rule: "AGENTS.md 设计契约：T+1 锁定"
    file: btcore/engine.py
    line: 1
    note: "触发输入；复现命令/测试；实际值；独立期望值与公式；影响"
```

位置为示例占位；必填 id/severity/rule/file/line/note，结构检查不代替证据核实。

```bash
python scripts/review_gate.py check findings.yaml --scope full
python scripts/review_gate.py check findings.yaml --scope diff
python scripts/review_gate.py close F-EXAMPLE-01
python scripts/review_gate.py done --full
```

- check exit 1：含历史未决 P0/P1，报告阻塞；修复验收后 close，或用户明确 waive。
- check exit 2：非法输入或 full 冷却期未过，不更新 backlog。
- check exit 0 / CONVERGED：登记发现无未决阻塞；范围完成即停止。P2/P3 新增不延长循环。
  不要求连续两轮或零发现；重复提交同一 YAML 不算独立审查。
- 同一 open ID 的新证据/分级变化更新原项；closed 项仅凭新的实质证据才重开。
  不因措辞或行号漂移重复报问题。
- full 至少间隔 14 天，脚本只记录仓库级日期；修复验收用 diff。
- 同一问题两次修复仍失败，停止自动改动，交付证据与方案。无新 diff/失败测试/
  新复现/用户扩大范围，不得再循环。

## done-bar 必须分层

1. 自动：反破坏 + skill 同步 + Ruff + 全量 pytest；记录 passed/skipped/failed。
   done 先拒绝未决 P0/P1，再跑两项机械检查；--full 才跑 pytest，Ruff 另跑。
   done 返回 0 仅代表已执行检查通过，默认调用会打印“测试未执行”。
2. 计算：固定代码/配置/数据窗口，独立重建受影响的现金、持股、费用、市值、指标；
   时序变更加未来扰动/截断证据。cross_validate 不能替代这些检查。
3. 实盘：已有券商快照对账证据或账本副本验证；缺证据写“未验证”，不写真实账本。

CONVERGED 和自动检查通过都不能代表完整计算/实盘认证；最终报告范围、阻塞项、
非阻塞待办、实际检查与未验证项。不得自动启动策略研究、重写基线或优化参数。
