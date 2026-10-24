# 2026-10-24 实盘 V2 重制交付记录

授权：用户确认旧实盘从未使用、允许彻底重制（对话记录），方案与验收矩阵见
[docs/live_redesign.md](../../live_redesign.md)。实施编排：S1 账户层与 S2
策略/计划层由 zcode/GLM-5.3 子代理完成（GLM-5.3 额度耗尽后由主控接替完成
S2 收尾与 S3 集成）；本记录对应最终工作区 diff。

## 范围

新增：`research/live_account.py`（账户事实层）、`research/live_plan.py`
（计划与执行层）、`research/live_strategy.py`（策略会话层）、对应 4 个测试
模块（account 81 / plan 42 / strategy 16 / CLI 15）。
重写：`research/live.py`（V2 编排门面）、`scripts/live.py`（V2 CLI 子命令）、
`scripts/live_e2e_check.py`（fixtures 离线全闭环演练）、
`tests/test_live.py`、`tests/test_review_duckdb_core.py`（V1 主体移除，
模块 docstring 记录旧→新保护映射）、`docs/live_guide.md`、
`.agents/skills/ddup-live-ops/SKILL.md`。
修订：`ARCHITECTURE.md`、`docs/index.md`、`docs/cli_and_research.md`、
`docs/review_backlog.yaml`（LIVE-16/17/18 登记后以 V2 回归关闭）。

## 关键语义决定（与方案的偏差/细化）

1. 组合事务：DuckDB 无 SAVEPOINT，采用深度计数 + rollback-only 毒标；
   嵌套失败即使被调用方捕获，外层也在 commit 前整体回滚（主控复现 rev2
   缺陷后由子代理 rev3 修复，`test_caught_nested_failure_aborts_outer_transaction`）。
2. 计划内 BUY 成交在入账同时登记 managed 政策与真实入场锚点，防止确认
   快照推进后策略自建仓退化为 observe 不可见。
3. 等权预算与回测 manual_buy 同口径（总资产/max_positions、现金顺序递减、
   卖出回款仅作依赖）；条件买入按策略请求的股数完整保留（不缩量），
   预留不足时警告并要求执行时按实际资金核验。
4. 同日重复接受：同快照不同内容拒绝（conflict）；快照已推进则返回既有
   计划（防止自身预留改变预览导致的双占用误判）。
5. 依赖卖单完全未成交前买单整体等待（`gate_order`），部分回款按成交比例
   解锁整手额度。
6. 首份对账单先立开仓基准再入事实（≤基准日仅审计）；后续对账单先事实后
   快照，差异即未解释差额。

## 验收命令与结果（2026-10-24，工作区 diff 未提交）

```text
.venv/bin/python -m pytest tests/ -q                        → 979 passed
.venv/bin/python -m pytest tests/test_live_account.py
    tests/test_live_plan.py tests/test_live_strategy.py
    tests/test_live.py -q                                   → 154 passed
.venv/bin/ruff check btcore/ tests/ scripts/ research/
    strategies/ factors/ adapters/                          → All checks passed
.venv/bin/python scripts/check_anticorrupt.py               → 通过
.venv/bin/python scripts/check_skill_sync.py                → 8 skill 一致
.venv/bin/python scripts/live_e2e_check.py                  → 离线全闭环通过
.venv/bin/python scripts/review_gate.py done --full         → 见最终报告
```

A01–A24 逐项映射：A01–07/A20 账户层用例；A08–19/A24 计划/会话/CLI 用例
（A09 预算、A11 依赖、A14 幂等/版本、A15 重启、A16 因果与缺口日、
A17 条件映射、A18 状态机、A19 原子）；A21 CLI 全流程与离线演练；
A22 全仓 979 通过 + 反腐检查；A23 LIVE-16/17/18 独立期望回归。
计算抽样：190 元净盈利、入金/出金 PnL 中性、部分回款解锁 100/200 股、
预算 5000+5000，均为手算独立期望（见各测试注释）。

## 验收与阻塞处置（2026-10-24 同日）

GLM-5.3-Flash 独立验收（全新上下文、只读）：A01–A24 静态逐项核对 21 PASS；
发现 LIVE-V2-01..05（报告存会话产物 live-v2/acceptance.md，要点已录入
backlog）。主控逐条裁定：01/02 确认 P1（02 经主控运行复现：订单 filled 200
vs 账本 1 笔成交），03 P2 成立，04/05 P3 成立；五项均当日修复并验收
（先建 RED 回归再修）：

| ID | 修复 | 回归 |
| --- | --- | --- |
| LIVE-V2-01 | `import-fills` 规范 CSV/YAML/JSON 成交导入 + 文档格式 | test_import_fills_csv_and_duplicate_report |
| LIVE-V2-02 | 重复成交回报先于终态守卫按 (source,trade_id) 查重，同内容幂等 no-op，异内容冲突；CLI fill 重复无害 | test_a19_duplicate_fill_report_is_idempotent_noop 等 3 例 |
| LIVE-V2-03 | 取消/过期/被替代后同日重接受返回稳定 state 错误 | test_cancel_plan_then_reaccept_clean_error |
| LIVE-V2-04 | live_guide 退出码口径与实现对齐 | test_error_channels |
| LIVE-V2-05 | statement/CSV 缺省成交日落库日期断言补齐 | test_statement_fill_default_date_persisted |

另按验收备注：`orders` 输出对带卖出依赖的在途买单附 gate 判定。
验收会话无执行工具，其 A21/A22 运行层由主控在同一工作区状态补齐：
全量 pytest 986 passed、Ruff 全范围通过、e2e 演练通过、
`review_gate done --full` PASS（五项发现已关闭）。

## 限制与未验证范围

- 无真实券商快照/接口：真实券商对账层未验证；模拟执行器仅覆盖
  拒单/部分/超时/恢复的离线路径。
- 回放成本 O(启用以来交易日)，未做真实规模性能基准；确认快照推进后
  的历史预览为"最新基准+压缩事实"口径（decision_hash 含快照版本）。
- 旧 V1 账本不迁移：明确拒绝（schema code），`scripts/diagnostics/`
  中引用 V1 接口的历史诊断脚本不再可运行（证据属 2026-10-19 快照）。
- GLM-5.3-Flash 独立验收：见同日验收记录（若存在）。
