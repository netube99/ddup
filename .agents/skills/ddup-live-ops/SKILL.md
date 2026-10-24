---
name: ddup-live-ops
description: 操作 ddup 实盘 V2 账本的建账、每日对账、策略启用、预览接受与执行回报；用于账本流程，不执行券商交易。
---

# 实盘 V2 账本操作

先读[实盘指南](../../../docs/live_guide.md#workflow)与
[重制契约](../../../docs/live_redesign.md)。确认账本路径、日期、券商输入与
用户授权；研究/排错在副本执行。写命令（import/confirm/activate/accept/
submit/fill 等）都可能写账；preview 与 status 只读。不连接券商、不下单。

## 每日操作

1. 准备券商对账单（现金/全量持仓/当日成交/出入金），按
   [输入格式](../../../docs/live_guide.md#input) 执行 `import`。
2. 核对输出：clean 自动确认；`stored_candidate` 按 `pending_diffs` 补
   `cashflow`/`adjust-shares` 后 `confirm`，或 `--takeover --reason` 显式
   接管（差异转为收益缺口，不阻止交易）。
3. `status` 查确认基准、投影、政策、在途订单与预留（sync 后立即生效）。
4. `preview` 只读核对次日操作单（预算、依赖卖单、条件触发价、跳过原因），
   可 `--trial-yaml` 试算；核对后 `accept` 或 `skip --reason`。
5. 按操作单人工执行：`submit` 确认已报单，`fill` 回报成交（费用逐项），
   `reject`/`cancel-order` 回报拒单撤单；提交结果不明先 `unknown`，
   查询核验后 `resolve` 恢复——不能盲目重发。
6. 次日 `import` 新对账单完成闭环；过期计划 `expire` 释放预留。

## 不可混淆的语义

- 券商快照是账户事实；现金差异必须显式分类（六类现金流）或接管，
  绝无自动 ADJUST，入金不产生投资盈亏。
- 成交幂等按 `(source, trade_id)`；不同 trade_id 的相同成交都保留，
  同 trade_id 异内容拒绝。计划接受绝不提前记成交。
- 持仓政策：managed（须显式入场锚点，不编造）/exit_only/observe
  （人工保留，仍计资产与资金占用）。
- 预览只读、可重复；策略版本在 activate 时固定，换策略须新 activation。
- 卖出回款只是买单依赖条件：未成交不可执行，部分回款按比例解锁。
- 预算被阻止（待确认差异/负可用现金）只影响新买入，卖出不受阻。

排错按[错误码表](../../../docs/live_guide.md#troubleshooting)；旧 V1 账本被
明确拒绝。真实账本修复须可追溯并在授权范围内；不为通过对账静音差异。

## 验证与停止

[离线演练](../../../docs/live_guide.md#verification)
`scripts/live_e2e_check.py` 用 fixtures 行情在专用临时目录跑全闭环，
不依赖真实行情、不触碰真实账本。交付处理日期、对账差异/接管、计划与
执行回报、未验证范围（真实券商层一律写"未验证"）。遇账本矛盾停止依赖
该状态的写入，明确所需证据，不反复改账直到通过。
