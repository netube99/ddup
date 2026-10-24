# 实盘 V2 指南：账户、会话与执行计划

[文档入口](index.md) · [重制方案与验收矩阵](live_redesign.md) · [架构](../ARCHITECTURE.md#ml-live-design)

实盘 V2 是本地、单账户、A 股日频的**手动执行工作台**：券商快照是账户事实，
策略通过会话生成只读预览，接受后成为带预算与依赖的执行计划，实际成交回报
驱动账本。不连接券商、不下单；轻度的操作与策略偏差（跳过、少买、延迟）是
设计内的正常输入。设计契约与 A01–A24 验收矩阵见
[重制方案](live_redesign.md)。

模块分层：账户事实 `research/live_account.py`（快照/成交/现金流/政策）、
计划与执行 `research/live_plan.py`（预算/依赖/订单状态机）、策略会话
`research/live_strategy.py`（启用版本/只读预览/重启重放）、编排门面
`research/live.py` 与 CLI `scripts/live.py`。

<a id="workflow"></a>
## 每日闭环

```text
券商导出/人工录入 → import（事实+快照）
  ├─ clean → 自动确认（status 立即可见）
  └─ 有差异 → 候选：分类解释（cashflow/adjust-shares）后 confirm，
              或 --takeover 显式接管（差异转为收益缺口）
preview（只读）→ 人工核对 → accept（计划落库+预算预留）
按操作单执行 → submit / fill / reject / cancel-order / unknown→resolve
次日 import 新对账单 → 循环
```

```bash
python scripts/live.py init live/main.duckdb --account-id main
python scripts/live.py import live/main.duckdb statement.yaml
python scripts/live.py status live/main.duckdb            # 只读，sync 后即新
python scripts/live.py activate live/main.duckdb --date 20260803 \
    --yaml strategies/selected/xxx/config.yaml --anchors anchors.yaml
python scripts/live.py preview live/main.duckdb [--date D] [--json]
python scripts/live.py accept  live/main.duckdb [--date D]
python scripts/live.py submit  live/main.duckdb --order <id>
python scripts/live.py fill    live/main.duckdb --order <id> --date D \
    --qty N --price P [--commission C] [--tax T] ...
python scripts/live.py orders  live/main.duckdb --plan <plan_id>
```

成功输出 JSON（`ok:true`）；错误输出 `{ok:false, code, message}`。退出码：
1=业务/校验/状态错误（含 `code=validation` 的内容校验），2=参数与文件系统
错误（文件不存在、非映射等）。`code` 稳定可脚本化（validation/conflict/
state/schema/not_found/unsupported）。preview 与 status 不写库。

<a id="input"></a>
## 对账单与建仓输入

statement YAML（`import` 一站式，字段见 `research.live.import_statement`）：

```yaml
date: "20260803"            # 快照日；非开市日先归一到最近开市日
cash: 41233.55              # 或 cash_balance/cash_available（二选一）
holdings:                   # 券商全量持仓，必填（空仓传 []）
  - {symbol: 600519.SH, shares: 100, available: 100}
fills:                      # 当日实际成交（可为空）；幂等按 trade_id
  - {symbol: 000001.SZ, side: SELL, quantity: 1000, price: 12.34,
     trade_id: "20260803-001", fee_commission: 2.47, fee_tax: 6.17}
cash_flows:                 # 分类现金流：deposit/withdrawal/fee/dividend/tax
  - {category: deposit, amount: 5000.0}
share_adjustments:          # 送转等到账股数：corporate_action/manual
  - {symbol: 600519.SH, delta_shares: 100, category: corporate_action,
     reason: "10送1"}
```

| 规则 | 行为 |
| --- | --- |
| 首份对账单 | 券商日终状态即开仓基准（opening）；其携带事实随后入账仅作审计 |
| 后续对账单 | 先入事实再导快照：clean 自动确认；差异存候选，不偷换正式状态 |
| 幂等 | 同 `(source, import_id)` 同内容重入无副作用；成交按 trade_id |
| 相同成交 | 不同 trade_id 的同价同量成交都保留；同 trade_id 异内容拒绝 |
| 现金差异 | 只能显式分类或接管；没有自动 ADJUST，入金绝不冒充收益 |
| 持仓政策 | `policy` 设 managed（须入场锚点）/exit_only/observe（人工保留） |
| 中途建账 | 允许；历史不完整只影响收益完整性（PnL incomplete），不阻止交易 |
| CSV 成交 | `import-fills` 规范 CSV（见下）；不冒充兼容所有券商导出 |
| 重复回报 | 同 `(source, trade_id)` 同内容重复 fill 回报幂等 no-op；同身份异内容拒绝。无券商编号时同日同价同量的多笔真实成交必须显式 `--trade-id` 区分 |

### 规范 CSV 成交导入（import-fills）

```csv
trade_id,date,symbol,side,quantity,price,fee_commission,fee_tax,fee_transfer,fee_other,note
20260803-001,,000001.SZ,SELL,1000,12.34,2.47,6.17,,,
20260804-002,20260804,600519.SH,BUY,100,1500.0,5.0,,,
```

首行表头必需；`trade_id` 缺省按 `{文件名}#r{行号}` 生成；行缺 `date` 时用
`--date` 缺省成交日（必填否则整批拒绝）；除 `note` 外选列可缺。异构券商
列名请先转换为本格式。YAML/JSON 用 `{fills: [...]}` 同字段。

<a id="operations"></a>
## 预览、计划与执行

- **preview** 只读：从启用边界确定性重放策略钩子，输出操作单（含依赖卖单、
  条件触发价、跳过原因与警告）。重复预览不推进策略状态；`--trial-yaml`
  试算其他策略不改正式会话。
- **accept** 把当前预览落为计划：同日重复接受幂等或明确拒绝（不双倍预留）；
  快照/策略版本变化后旧预览不可接受，须重算。`skip --reason` 记录跳过审计。
- **预算**：等权口径与回测一致（总资产/max_positions，现金顺序递减）；同批
  支出+费用不超过可用现金减已有预留；卖出回款只是依赖条件，未成交不解锁
  买单，部分回款按比例释放（`gate_order`）。
- **条件单**：STOP_LOSS/TAKE_PROFIT/TRAILING_TP 转为带触发价与有效期的
  保护单（互斥组：触发其一撤同组其余）；已触发的 ML_EXIT 转为明确卖出；
  不支持的类型携带完整参数显式拒绝（`UnsupportedConditionError`）。
- **订单状态机**：planned→submitted→partial/filled/cancelled/rejected；
  提交超时置 unknown，只能 `resolve` 查询核验后恢复，不能盲目重发。
  每单有稳定 `client_order_id`（`{plan_id}#{seq}`）。`orders` 输出对带
  卖出依赖的在途买单附 `gate` 判定（依赖卖单未成交即不可执行、部分回款
  按比例解锁的整手额度）——回报买单成交前先看 gate。
- **原子性**：成交回报与账本事实同一组合事务；任一步失败整笔回滚
  （嵌套失败即使被捕获也整体 rollback-only）。

<a id="troubleshooting"></a>
## 排错

| 现象 | 下一步 |
| --- | --- |
| `code: validation` | 按 message 修正输入（日期/数值/必填字段） |
| `code: conflict` | 同身份重复导入或同日重复决策；按 message 区分幂等场景 |
| `code: state` | 状态机拒绝（未提交先回报、unknown 重发、接受旧版本） |
| `stored_candidate` | 看 `pending_diffs`，补 cashflow/adjust-shares 后 confirm |
| `blocked_reason`（预算） | 待确认快照差异或负可用现金；先对账/接管/补事实 |
| PnL incomplete | takeover_gap 属收益口径说明，不阻止交易；缺价补收盘价 |
| 预览拒绝（版本不一致） | 策略文件/配置改动后需新 activation 固定版本 |

旧 V1 账本（`ledger_meta` schema）会被明确拒绝且不写坏文件；V1 的
sync/signal/回放主路径已移除，迁移说明见
`tests/test_review_duckdb_core.py` 模块 docstring。

<a id="verification"></a>
## 回归与演练

```bash
.venv/bin/python -m pytest tests/test_live_account.py tests/test_live_plan.py \
    tests/test_live_strategy.py tests/test_live.py -q
.venv/bin/python scripts/live_e2e_check.py     # 离线全闭环演练（fixtures 行情）
```

`live_e2e_check.py` 用仓库 fixtures 行情在专用临时目录驱动 CLI 子进程完成
init→import→activate→accept→submit→fill→次日对账→preview→expire 全链路，
不依赖本机真实行情库，不触碰真实账本。真实券商对账证据仍需以实际
账户快照核验，未执行时如实记录"未验证"。
