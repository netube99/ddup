# 架构问题 1 / 2 修复验收（2026-10-19）

## 范围与基线

- 用户明确要求两个子代理分别修复问题 1 / 2，即 ARCH-LIVE-01 与 ARCH-RUN-01。
  ARCH-RUN-01 虽为 P3，已获本轮明确修复授权。
- 起始 commit：`9d258bad87c8d25365b40a453270154f602c68c0`。
- 起始工作区已有本日架构报告、findings、合成证据脚本，以及 backlog/历史索引改动；
  均保留。历史初审证据不改写为修复后的结论。
- 子代理一负责实盘回放、独立回归和实盘指南；子代理二负责运行清单、持久化回归及
  结果文档。主代理检查直接调用链、集成 diff、更新证据与执行最终门禁。
- 范围为缺行情日日生命周期与回测运行身份；不扩展为数据准备重构、类型体系重构、
  性能优化或全仓计算审查。
- 真实账本、私有策略/模型、研究基线及历史结果库不写入；核验用合成行情、fixtures、
  内存账本和临时结果库。

## 验收计划与完成条件

1. ARCH-LIVE-01：先证明旧实现违反三日计数和退出预期；修复后覆盖空行情日订单清理、
   解锁、无决策钩子、公司行为/现金股数不变量、恢复交易及前缀一致性。
2. ARCH-RUN-01：先证明不同生效因子权重的运行记录无法区分；加入版本化运行清单，
   覆盖实际声明、内容身份、弱/未知数据版本边界，保持原平铺配置和旧结果可读。
3. 检查修复 diff 及直接调用链；更新原 ID 的修复证据，通过针对性验收后关闭两项。
4. 运行 `python scripts/review_gate.py done --full` 和全范围 Ruff，分别记录实际结果。
   全量 pytest 的计数由 JUnit 输出保留，不能用旧基线数量代替。
5. 无未决 P0/P1 且本轮范围验收完成后停止。其他 backlog 保留。

## 实现与独立证据

### ARCH-LIVE-01

`replay_ledger` 每个交易日调用共享 `value_account`；`run_decisions=True` 时每天调用
`compute_pending`。引擎已有空行情分支负责计日、解锁、清空 pending/条件单并提前返回，
不会调用决策钩子。保留 `run_decisions=False` 不推进策略决策态的语义。

子代理先建立入口回归，旧实现持仓天数为 2，独立期望为三个交易日计 3 天；修复后通过。
主代理要求补核缺行情当天状态时，发现同入口还在跳过 `value_account`：公司行为已将现金
和股数更新，日总资产仍为 10,000。新增失败断言后的第二个最小改动是每日估值结算。

合成账户起初现金 9,000、100 股、每股 10；缺行情日每股派现 0.5，短期税率 20%，
同时送转 10%。独立计算为：

- 现金：9,000 + 100 × 0.5 × 0.8 = 9,040。
- 股数：100 × 1.1 = 110；沿用公司行为调整后的 last_price = 10 / 1.1。
- 总资产：9,040 + 110 × (10 / 1.1) = 10,040；当日盈亏 40。

缺少 pre_close 的既有告警仍存在，没有引入启发式现金除息价格补偿。这里验证账户恒等式
与当前估值契约，不宣称缺失行情时能够恢复真实交易所收盘价。

回归还直接检查缺行情日解锁、清空条件单和 pending，四类策略钩子只在有行情日调用；
恢复交易后计数 3、到期卖单存在。短窗与包含未来极端价格的长窗共有账户前缀一致，
逐日现金、市值与盈亏关系成立。`tests/test_live.py` 67 passed，
`tests/test_review_calculation_boundaries.py` 28 passed。

### ARCH-RUN-01

新增 `btcore.run_manifest`；`Engine.run` 在 prepare/策略钩子前构造版本 1 清单，作为
`config_json._run_manifest` 保存。顶层原配置字段及 CLI 资金/仓位覆盖值保留，数据库
DDL 不变。GenericSQLBackend 公开数据路径，用于弱来源标识。

清单包含：实际解析的执行配置、因子规格/过滤规则/因子闭包、策略类和文件哈希、引擎文件
哈希、Git commit 与依赖版本、模型文件哈希和完整已加载特征/scaler/meta、数据源路径
及文件属性或后端快照声明。已固定的执行参数在构造后 config 发生变化时仍按实际值记录。
无 Git 时软回退；无数据版本时标 unknown，路径/size/mtime 只标 weak。

修前证据来自本日初审运行的原始探针：生效权重为 1/2，保存的 config_json 却相同。
修后主代理重跑同一探针，记录权重为 `[1.0, 2.0]`、config_json 不同，契约满足。
子代理首次 RED 尝试仅因新模块尚不存在而 ImportError，**不计作功能失败回归证据**；
这里明确使用初审的旧实现动态结果作为修前证据。

新增测试覆盖 Engine 两次落库、声明变动、完整模型身份、显式执行参数、CLI 覆盖、
构造后配置变动、NumPy 标量、无 Git、真实临时文件的弱标识、保留键冲突及旧结果经
`report.load_runs` 读回。与 engine hardening / strategy loader 合计 99 passed。

新增序列化边界见结果指南：不再将任意 Python 对象隐式 `str()`；不支持值、非字符串
mapping 键及 NaN/Inf 会 fail-fast；`_run_manifest` 是保留键。该变化不修改交易计算，
但使用这些配置值的自定义调用方需转换为已支持类型。

## 检查与范围限制

主代理确认两项修复的直接调用链和定向证据后，以原 ID 更新修复证据并执行 `close`。
登记更新时 exit 1 是关闭前未决 P1 的正常状态；两项均按已修复关闭，没有 waive。
全量验收结果补在下表；子代理开发过程曾因共享 Engine 中间编辑顺序错误出现临时失败，
修正后两边定向套件重跑通过，该中间输出不作最终结论。

| 检查 | 实际结果 |
| --- | --- |
| live / 计算边界定向回归 | 67 + 28 passed |
| manifest / 引擎加固 / loader 定向回归 | 99 passed |
| 架构合成探针 | 两项契约满足；ARCH-PERF-01 重复工作量保持原结果，未优化 |
| 全范围 Ruff | passed |
| `git diff --check` | passed |
| `review_gate.py done --full` | exit 0；反破坏、skill 同步通过；全量 pytest 871 passed，0 failures/errors/skipped，75.254 秒 |
| 真实行情独立重算、真实券商对账 | skipped，本轮没有读写真账本 |

完整门禁通过时使用 `PYTEST_ADDOPTS='--junitxml=/tmp/ddup-architecture-repairs-20261019-pytest.xml -ra'`
保留测试计数，主代理从 JUnit 读取上述实际结果。最后仅更新记录并复核 skill/链接和 diff，
没有重复运行全量测试。当前 open：P0=0、P1=0、P2=25、P3=32；ARCH-LIVE-01、
ARCH-RUN-01 均已 resolved，ARCH-PERF-01 保留。`last_full_audit` 仍为 2026-10-12。

历史回测收益计算没有变化，无需因本轮修复全部重跑。受缺行情影响的旧实盘 signal
衍生结果如需更正，应先在账本副本重放；未自动改真实账本。新清单不能为旧 run 凭空
恢复声明或数据版本，旧结果仍可读但追溯信息不完整。

清单不是完整可重放快照：不包含任意策略运行态、全部动态依赖或行情内容；后端 snapshot_id
是来源声明，不是引擎独立认证。实盘衍生库仍按账本契约保存，未套用回测清单。

修复证据：[findings](architecture_repair_findings.yaml)、
[合成输出](architecture_repair_evidence.json)、
[可执行探针](../../../scripts/diagnostics/architecture_evidence_20261019.py)。
