# Backlog 清理整合记录（2026-10-24 批次）

## 范围与规则

对 `docs/review_backlog.yaml` open 区 83 条未决项做一次性清理整合；不重开全仓审查。
各条目的技术判定由七个组件批（内核 23、因子机制 11、因子定义 A 11、因子定义 B 10、
研究层 7、策略层 9、CLI/文档 12）分别给出探针/DB/回归证据；整合核对
ID 覆盖恰好一次、按处置落地并跑门禁，主控最终核对剩余项及报告。

用户规则：确认可安全修复的问题修复；失效或无法确认且改动风险无依据的记录直接从
open 删除（不标 waived、不伪装 fixed、不另建待办）；性能/加固类建议无实际收益证据
不实施。内核 F-STA-01/F-INF-03 已撤回推测性非正权益守卫，按删除处置，禁止重新实施
（负权益年化/周期收益无独立可证口径，掩蔽 0.0 属发明财务口径）。

## 处置汇总

起始 open 83 条：fixed 57（含代码修复、文档纠偏与验证补齐，
`review_gate close` 正常关闭，resolved 2026-10-25，无 waive）、delete 26
（经用户授权直接删除）、剩余 open 0。
closed 区原有 161 条（含主控实盘 V2 处置 LIVE-V2-02/06/07/08）原样保留，现 218 条。

fixed（57）：F-ATT-02 F-ATT-03 F-ATT-04 F-CHK-01 F-CLI-01 F-CLI-02 F-CLI-03 F-CLI-04
F-CLI-05 F-CLI-06 F-CLI-07 F-DIAG-01 F-ENG-03 F-FAC-08 F-FAC-09 F-FD0-03 F-FD0-04
F-FD1-03 F-FD1-04 F-FD2-06 F-FD3-04 F-FD3-05 F-FD3-08 F-FD3-09 F-FD4-05 F-FD4-06
F-FD4-07 F-FD4-08 F-FD4-09 F-FD5-02 F-FD5-04 F-FD5-06 F-FD6-04 F-FD7-01 F-FDX-03
F-FDX-06 F-FEV-01 F-FEV-02 F-INF-04 F-INF-05 F-INF-07 F-MAT-03 F-ML-04 F-ML-07
F-ML-08 F-RSR-04 F-RSR-05 F-RSR-08 F-RSR-09 F-RSR-11 F-RSR-12 F-STA-03
F-STO-01 F-STR-01 V-ENG-01 V-ENG-02 V-MRD-01

delete（26）：F-RSR-10 V-INFS-01 E2E-02 ARCH-PERF-01 F-BKD-04 F-ENG-04 F-FAC-03 F-FAC-04 F-FAC-07 F-FD2-07
F-FD5-03 F-FDX-01 F-FDX-04 F-FDX-05 F-INF-02 F-INF-03 F-INF-06 F-MAT-04 F-ML-05
F-ML-06 F-RSR-06 F-STA-01 V-FAC-01 V-FAC-02 V-MLS-03 V-MRD-02。删除理由集中在三类：
引用的文档表述已不存在（漂移已收敛）、路径不可达且无收益证据的防御/性能建议、
行为已正确的口径局限登记。逐条证据见各组件批报告（会话工件）与代码内 F-* 锚点。

主控收尾裁决：删除 E2E-02。实际读取 `ml_alpha_300/config.yaml` 确认已有
2026-09-19 注记，明确历史研究数字不能代表当前早停 artifact；原记录所指的
业绩声明问题已被说明。重训与增加树数/best_iteration 元信息属于研究或维护建议，
本轮没有据此证明当前训练/推理实现错误。按用户要求不改模型、不重训、不发明修复，
直接删除记录，不标记为已修复或 waived。该处置不认证旧业绩数字或模型有效性。

## 历史结果重跑影响

- `rep_intensity`/`w_rep_intensity`：绝对值口径修正为元/万元（旧值大 1e4 倍），按绝对
  阈值或 ML 消费的历史结果作废需重跑；截面排序不变，库内无在用消费者。
- `core_lowvol_500`（timing_gate 开启）：门控历史中位由 59 值改 60 值，按声明口径复现
  需重跑（仅边界 ±1 值差异）。
- 归因报告重新生成时口径变化：基准独有行业明细行、期间平均权重（effective_n 等）、
  停牌日基准计入——均属修复目的；cross_validate 仅 rate=0 配置行为改变，默认费率等价。
- `volume_impulse`/`turnover_chg`/`mom_diverge`/`dist_to_limit`/`cvw_mkt_gate`/`elg_lg_div`：
  仅原垃圾行（inf/哨兵/假分歧/门控 0）变 NaN 或常数倍修正，正常行不变，库内无在用消费者。
- F-RSR-08 六个 exploring 策略的既有数字不代表修正后的交易日计数行为；若继续使用
  这些历史数字，仍需重跑。未为本次清理改写研究结果库。
- 内核与因子机制的验证覆盖列出的合成场景和既有回归，不能推广为所有正常输入的逐位等价。

## 证据强度核对与撤回

用户追问改动质量后，主控核对行为 diff、组件证据和新增回归，撤回两项：

- F-RSR-10：旧公式会退化为固定宽度有证据，但把 vol_ratio 乘 100 没有独立的策略
  规格依据；测试直接断言新单位，不能证明新单位正确。恢复两个示例原有计算及默认值，
  移除本轮新加的三个针对百分点口径的测试。保留独立的列声明和列表遍历修复。
- V-INFS-01：缺失买入成本的超卖净额不能据此确定已实现利润；新用例的费用字段与
  net_amount 不一致，期望 975 也不同于原记录的 970，缺可靠账本对照。恢复原有
  统计行为和告警，撤回新输出键及其测试/文档，不改变既有回归。保留基准价格列修复。

这不是把失败测试删掉来通过门禁：两项撤回前的测试原本都能通过；撤回的是未经充分
证明的行为与为该行为新写的断言。两条记录按用户指示删除，不继续声称已经修复。
AST 比对确认三个统计函数和两个止损 handler 与 HEAD 原实现相同。

证据分层：归因平均权重、单位换算等有独立公式/原始数据样本与回归；输入校验和 CLI
有明确契约及拒绝/错误通道测试；文档类核对现行代码与数据定义，不需要数值测试。
同一子代理编写实现和测试的部分仍非独立验收；主控没有逐项重做全部原始数据核算。
不把“有对应测试”“全量通过”写成“每项已独立证明”或全局无风险结论。

## 实际检查

命令与结果（仓库根，`.venv`）：

- `python scripts/review_gate.py done --full`：自动检查 PASS（反破坏、skill 同步、
  全量 pytest 通过；门禁对 pytest 仅显示 PASS 行）；剩余验收清单照常打印。
- `ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/`：
  All checks passed。
- `git diff --check`：通过（无空白错误）。
- `python scripts/live_e2e_check.py`：PASS（离线实盘 CLI 全闭环）。
- 最终核对 open=0、57 条正常关闭、26 条删除，原有 161 条 closed 内容与清理前逐项相同。
  删除条目未伪装成修复或 waive；本轮开始前的实盘修复 diff 保持完整。
- 两项撤回后：受影响的统计、策略、示例加载、策略端到端及实盘回归合计 73 passed；
  全范围 Ruff 与 diff 空白检查通过。最终 backlog 核对为 57 条关闭、26 条删除、open=0，
  原有 161 条 closed 内容不变。随后 `review_gate done --full` 再次 PASS（反腐、
  skill 同步、完整 pytest 均通过；门禁没有输出测试数量，不沿用撤回前的 1088 数字）。
- 撤回两项前的全量 `pytest tests/ -q`：1088 passed / 0 failed（约 108s；门禁内部对 pytest 仅显示
  PASS 行，统计为整合者单独运行所得；首跑曾现 2 例失败，系旧夹具
  `tests/test_review_duckdb_backend.py` 的 `index_member_all`/`sw_daily` 缺
  `l1_code`/`ts_code` 列，被 F-CLI-01 对齐消费方口径的修复暴露，补齐列后全绿）。

## 局限

- 未做真实券商对账验证；实盘检查仅在离线 e2e 闭环。
- `factors/library.yaml`、`strategies/exploring`、`strategies/selected`、`adapters/tushare.py`
  被 .gitignore 排除（用户扩展区），其修复已落盘但不纳入本次提交/推送；本机测试包含
  这些资产。提交包含可分发机制、示例、测试与文档，不宣称全量测试在缺少本机资产的
  全新 checkout 中可直接复现。
- 清理结束时 open 为空；删除疑点不等于证明其所有相关行为正确，保留已文档化的使用边界。
