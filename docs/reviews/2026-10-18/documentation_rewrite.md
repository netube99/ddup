# 文档体系与 skill 重写（2026-10-18）

## 范围与基线

- 起始 commit：`67abff3dc22fcf0bee80650acc236c41f0fddc97`；开始前工作区已有一轮
  已授权的文件归类和审查记录迁移，本次保留并继续。
- 范围：README、AGENTS、ARCHITECTURE、docs 当前指南与历史导航、八个项目 skill、
  `scripts/check_skill_sync.py`、私有 `docs/devlog/` 的失效内容。
- 未修改 `btcore/`、策略、因子、模型、研究基线、结果库或实盘账本。
- 完成条件：按任务可导航；当前接口与代码一致；删除/合并重复和流水账但保留设计边界、
  历史 ID 与证据；skill frontmatter、引用、CLI 和代码契约检查通过；自动回归通过。

## 处置

- 建立“首次运行 / 架构 / 专题参考 / 研究 / 审查”分层；`docs/index.md` 按任务和设计问题
  双重路由，README 与 AGENTS 不再各维护一份全集。
- 因子、后端、策略、CLI、结果分析拆成单一职责指南；新增因子研究、策略模式、ML、实盘、
  开发入口，合并重复教程、参数表和结果库 schema 说明。
- 修正文档与当前实现不一致的生命周期、provider 边界、select 冲突、过滤器回退、
  compute_breadth 内存/组均值、统计和实盘写入语义。
- 八个 skill 按任务触发、最少步骤、危险点、输出和停止条件重写；详细接口以稳定锚点链接到
  专题文档，移除强制连续研究、经验阈值和“唯一正确路线”等普遍化要求。
- 历史审查按日期完整迁移；旧全量核查压缩为发现、独立期望、数值和限制，不再保留
  并行分工、临时 `/tmp` 步骤等流水账。
- 私有 devlog 合并 ML 假设、压缩重复策略诊断、保留高密度历史表，并删除已被当前实现
  取代的审计派工单。
- `check_skill_sync.py` 改为解析稳定锚点和代码真实符号，同时检查 skill frontmatter、
  本地链接/锚点、CLI flags、算子表、select/filter/conditions、引擎默认值和 ML meta。

## 末轮事实校正

重写后再核对的漂移点（均改文档侧，不改代码）：

- `docs/strategy_guide.md`：`Strategy.on_tick` 基类并非空实现，它先
  `ConditionBuilder.prune(持仓集合)` 再返回 `None`；覆盖时必须调用 `super()`。
- `docs/strategy_guide.md` + `docs/backend_guide.md`：指数成分告警分两处独立发生——
  preload 裁剪（缺方法告警一次，有方法无快照静默回退全市场）与 `StockFilter` 逐日白名单
  （缺方法、无数据各在构造时告警一次）。原“告警一次、规则不生效”的单一表述已拆开。
- `docs/backend_guide.md`：`tables.date` 只覆盖 panel 表、ST、指数成分、基准表的日期列；
  日历与分红的日期列绑在 `calendar_date` / `dividend_ex_date` 表单条目上，写 `tables.date`
  不生效也不报错。
- `docs/ml_guide.md`：章节号改为连续 1–6；`runs.config_json` 的 `models_meta` 只含
  `name/scope/label/train_window/artifact_sha256`，因子定义与因子库版本不落盘；新增
  “已知边界”节，登记 `F-ML-04`–`F-ML-08` 的现行行为。
- `docs/review_backlog.yaml`：`rule`/`note` 中的旧章节号标注为 `原§N`，`review_protocol`
  与 `review_gate.save_backlog` 的文件头同时声明：旧章节号与已停用的 checklist ID 都是
  历史定位符，定位当前文本改用引号内原文。发现条目本身不改写。
- `scripts/check_skill_sync.py` 权限恢复为 `100755`；`.gitignore` 补回 `market.db` 与
  `!tests/fixtures/*.parquet`。

## 独立审查（同日，只读 reviewer 子代理）

范围：工作区未提交 diff（`67abff3` 起），只读核对文档与代码、链接、历史迁移完整性，
不修改文件。结论 `VERDICT: PASS`，无 P0/P1。

已核对一致：`Strategy.on_tick` 基类 prune 行为；指数成分两处告警阶段；`tables.date`
对日历/分红无效；`models_meta` 五个字段；F-ML-04/05/08 的现行行为；21 个算子、7 个
select 键、13 个必需表单位置、8 个不变量测试等计数；backlog 编辑只动 `rule`/`note`
与路径，未改 id/severity/opened/resolved；`docs/reviews/README.md` 覆盖全部 11 个迁移
文件，仓库无失效的 `docs/review_*_2026*` 链接。

登记的非阻塞项（已入 backlog，均 P3，本轮不修）：

- `F-DIAG-01`：`scripts/diagnostics/review_evidence_20261012.py` 写死本机行情库路径，
  缺配对的 `--market` 覆盖；历史迁移原样带入，影响证据命令跨机可复现。
- `F-CHK-01`：`check_skill_sync` 的 CLI 对账只覆盖与 `scripts/` 同行书写的 flag，
  覆盖窄于 `docs/development.md` 的文字（现存裸 flag 均真实有效，无失实声明）。
- 审查还指出 backlog 中引用的已停用 checklist ID 同样不可解析；处理方式是把这条定位
  约定写进 `docs/review_protocol.md` 与 `save_backlog` 生成的文件头，而不是逐条改写历史
  发现。

未覆盖：审查方未跑 pytest/review_gate（按分工由主会话执行），未重算
`results_analysis`/`factor_research` 的数值结论，未执行 skill 流程文本，未解析
YAML 语义（只读结构）。

## 验收

| 检查 | 实际结果 |
| --- | --- |
| 八个 skill 的 `quick_validate.py` | 8/8 `Skill is valid!` |
| `scripts/check_skill_sync.py` | `OK：8 个 skill、代码契约与本地链接一致` |
| `scripts/check_anticorrupt.py` | `反腐检查通过` |
| Ruff（`btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/`） | `No issues found` |
| 全量 pytest（`pytest tests/ -q`） | `859 passed` |
| `git diff --check` | 无输出（通过） |
| `scripts/review_gate.py done --full` | `[PASS]` ×3（反破坏、skill 同步、全量测试） |

本轮未验证：真实库 `cross_validate`、真实策略基线重跑、模型训练/导出、券商对账与
实盘账本；`done --full` 的退出码只覆盖本次已执行检查。

本次是文档与工具维护，不重跑真实策略基线、模型训练、券商对账或生产交易。历史数字仍有
原记录的数据/版本限制；自动测试通过不能替代真实账户或全部研究输入的验证。
