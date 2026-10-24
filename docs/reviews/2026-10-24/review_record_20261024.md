# 2026-10-24 核心部位代码质量审查记录（子代理串行编排）

基线：commit `a732e1d`，工作区零改动。审查全程只读，仓库未做任何修改；
逐 lane 报告与探针在本机 `/tmp/ddup-review/<lane>/`（易失，未拷入仓库资产——
探针含本机绝对路径，遵循 F-DIAG-01"私有/本机路径不得进入仓库资产"口径；
关键 P1 复现代码已内联本文）。发现汇总见同目录
[review_findings_20261024.yaml](review_findings_20261024.yaml)（note 前缀为 lane 名）。

## 方法与授权范围

用户授权对四个核心部位（撮合内核、因子层、策略层、研究工具集）做代码质量
检查与优化分析；因账号并发限制，7 条审查 lane 按模块依赖自底向上**串行**执行，
每次仅 1 个子代理运行。6 条复杂 lane 用 zcode/GLM-5.3:max，CLI 薄壳 lane 用
zcode/glm-5.3-flash:max。

| # | lane | 主审范围 | 模型 |
| --- | --- | --- | --- |
| 1 | match-core | btcore/match/*、engine.py、corporate/costs/slippage/limits | GLM-5.3:max |
| 2 | engine-support | btcore/stats.py、database.py、run_manifest.py、provider.py | GLM-5.3:max |
| 3 | factor-layer | btcore/factors/*、factors/library.yaml | GLM-5.3:max |
| 4 | strategy-layer | btcore/strategy.py、strategy_loader.py、strategy_tools.py、filters.py、strategies/（archive 除外） | GLM-5.3:max |
| 5 | research-quant | research/factor_eval.py、composite.py、cross_validate.py、sweep.py、cli_common.py | GLM-5.3:max |
| 6 | attr-report | research/attribution.py、report.py、replay.py | GLM-5.3:max |
| 7 | scripts-cli | scripts/ 11 个 CLI（live/ml/devtool 除外） | glm-5.3-flash:max |

发现硬标准（review_protocol 五要素）：触发条件、可运行复现、实际值、独立期望值
及依据（手算/独立公式/书面契约，禁止被测实现自证）、受影响对象；纯风格与
未复现推测不收。全部 28 条与 docs/review_backlog.yaml 逐条查重，无重报、无撞号。

**排除范围**（未审，非遗漏）：ML 域（btcore/ml、scripts/ml_train.py）与实盘域
（live/、scripts/live*.py、research/live.py）按用户指示另行安排；数据层
（adapters/、btcore/generic_sql.py、backend.py）2026-10-24 刚完成验收（commit
a732e1d/2cb2fe4）无新 diff 不重审；开发工具（scripts/review_gate.py、check_*、
diagnostics/）2026-10-18 已审；tests/ 为参照非对象；strategies/archive/ 跳过。

## 发现汇总：28 条（P0×0、P1×4、P2×8、P3×16）

### P1（支持路径已复现的错误计算/成交/契约违背）

| ID | 位置 | 一句话 | 主控核验 |
| --- | --- | --- | --- |
| F-CST-01 | btcore/slippage.py:8 | `round()` 银行家舍入偏离自述"四舍五入到分"与 limits.py 同口径 Decimal HALF_UP；dyadic 半分位（x.125/.375/.625/.875）成交价每笔偏 1 tick | 复跑 probe_01 + 独立 Decimal 对照确认 |
| F-STA-02 | btcore/stats.py:189/578 | `_compute_management_complexity` 与 `cost_per_trade` 用未过滤 trades，STK_DIV/ADJUST 公司行为行计入成交笔数（:140 自注 exec 口径未跟进两处） | 复跑探针 + 代码现场确认 |
| F-RSR-07 | strategies/selected/value_momentum/strategy.py:77 | factor_specs 闭包不引用 idx_ret → 伪列不附着 → `bar.get("idx_ret",0) or 0` 恒 0 → 第 30 个决策日起 `_bull_mode` 永久 False，在产策略"基准趋势感知"锁死熊市权重 | 复跑探针（needs['index']=False、_bench_returns={0.0}）+ config 现场确认 |
| F-ATT-01 | research/attribution.py:258 | 持仓重建按 `ORDER BY date,id` 回放，引擎把公司行为行追加在同日成交之后（id 更大），违反"同日公司行为先作用"契约（constants.TRADE_EVENT_PRIORITY、docs 原句）；除权日同日成交时股数被 STK_DIV"设为总数"语义覆盖/复活，此后归因持续失真 | 复跑探针（真实引擎 fixtures run：隐含持股 23660/48460 vs 期望 0/24800） |

F-CST-01 最小复现（纯函数，仓库根执行）：

```python
from decimal import Decimal, ROUND_HALF_UP
from btcore.slippage import apply_slippage
p, t, d = 8.625, 0, -1
print(apply_slippage(p, t, d))  # 实际 8.62（round 半偶）
print((Decimal(repr(p)) + d * Decimal(t) * Decimal("0.01"))
      .quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))  # 期望 8.63
```

备注（登记时随 ID 保留）：F-CST-01 端到端示例中 10.10×0.95 触发价类偏差一半成因
是上游浮点乘法落在 9.574999…，仅修 slippage 只消除 dyadic 半分位类；彻底对齐
"十进制意图值"需 ConditionBuilder 侧 Decimal 化，修复方案需覆盖该边界。

### P2（证实局限/研究工具口径缺陷）

| ID | 位置 | 一句话 |
| --- | --- | --- |
| F-STA-01 | btcore/stats.py:220 | 月度/年度区间收益与年化对非正权益缺守卫（对照 daily 主路径有 where= 守卫）；inf/NaN 经 json.dumps 默认 allow_nan 入 stats_json（非严格 JSON） |
| F-FD7-01 | factors/library.yaml:953 | elg_lg_div 缺 ×10，F-FD1-02（closed，修 143-158 行六因子）漏网的第七个同族因子；值比自述"净占比差"小 10 倍，绝对阈值消费方错 10 倍 |
| F-RSR-08 | strategies/exploring/（6 处） | E2E-01（closed）同型 int(YYYYMMDD) 日期算术残留：4 策略调仓跨月强插、re_attack_500/dv_tg_barbell 冷却期 1 日即过期 |
| F-RSR-09 | strategies/selected/trend_guard_bw_300/strategy.py:102 | bear_gating 开启时因 pct_above_ma20 坍缩列未物化而静默失效（当前 config 未开启，故 P2） |
| F-MRD-03 | research/cross_validate.py:277 | `len(trades)` 把 DIV/STK_DIV/ADJUST 行计入总成交/日均，HIGH_FREQ 可被公司行为行推过阈值误报（F-STA-02 同根因研究层残留） |
| F-ATT-02 | research/attribution.py:473 | industry_detail 缺基准独有行业行，Σ明细与 summary 差 = 低配行业 allocation 效应，无法对账 |
| F-ATT-03 | research/attribution.py:536 | exposure 平均权重用条件 mean()（跳过未持有日），Σ权重可 >1，effective_n_industries 可 <1（0.5 vs 期望 2.0） |
| F-ATT-04 | research/attribution.py:244 | 日期轴取 bars 行集，集中持仓组合整体停牌日整日从 Brinson 窗口消失（基准收益静默少计） |

### P3（防御缺口/教学示例失实/错误通道/口径观察）

F-STA-03（stats 基准取列绕过共享口径）、F-FAC-09（weight inf 通过校验）、
F-STR-01（非正总权重静默退化为全 1.0 均匀分）、F-STO-01（字符串 index_universe
拆单字符静默回退全市场，F-RSR-02 机制缺口）、F-RSR-10（condition_hunter"自适应
止损"数学上恒 3%）、F-RSR-11（turnover_rate 未声明 REQUIRED_FIELDS 恒取 0）、
F-RSR-12（core_lowvol 择时窗口 59 vs 声明 60 日）、F-FEV-01（calc_factor_corr
常量日 NaN 跨日传播杀死整窗均值）、F-FEV-02（--forward 负数无校验，回顾口径被
当前瞻 IC 呈现）、F-CLI-01…07（导出确定性/前置校验顺序/裸 traceback 错误通道/
bench None 崩溃/board 脚本 idx_ret 族不可用等）。逐条证据见 findings yaml。

### 主控核验状态

28 条中 25 条由主控独立复跑探针/复现命令确认（含全部 P1×4、P2×8）；3 条 P3
（F-CLI-01/05/06）为同构错误通道类，采信 lane 报告内完整命令输出，未独立复跑。
查重：28 个新 ID 全文检索 backlog 零碰撞；在册 open 项（F-FAC-03/04/07/08、
V-FAC-01/02、F-FDX-01~06、F-FD0~FD6 波次、F-MAT-03/04、F-ENG-03/04、V-ENG-01/02、
F-INF-03、V-INFS-01、F-BKD-04、V-MRD-01/02、F-RSR-04/05/06、E2E-02、ARCH-PERF-01
等）与 closed 项（F-FAC-01/02/06、F-MAT-01、F-ENG-01、F-INF-01、F-RSR-01/02/03、
E2E-01、F-FD1-02、F-RPT-01~08、966d587/67abff3 修复批次）均未重报。

### 验证通过项（负证据，摘录）

T+1 时序/锁仓/资金顺序/现金守恒/除息 trailing rescale 探针四场景 + 109 例既有
测试（match-core）；F-INF-01 修复后 fuzz 300/300 守恒（最大偏差 2.33e-09）、
provider 四项锚点契约实测、database 三条失败路径（engine-support）；算子数值/
CSE 等价/分块一致独立对拍（factor-layer）；IC/RankIC/分层/next-open 前向对齐/
composite 因果权重独立手算对拍全部正确（research-quant）；Brinson 主数学真实库
端到端 20/20 日 Δ≤1e-9（attr-report）；CLI 接线/只读模式/退出码与 docs 一致、
相关 pytest 93 例全绿（scripts-cli）。

## 待验证（不计 findings，无书面契约依据或需真实库）

match-core 6 条（手动买 volume-cap 截断零告警、pending 条件买单价不随除权
rescale、rescale 价值守恒口径 vs 交易所参考价等）；strategy-layer 4 条
（subjective_hold 账本连接、指数快照首期前视边界等）；research-quant 1 条
（composite equal/ic 对缺失因子值语义不一致，有探针证据无契约可引）；
F-CLI-01 的同名行业重复组数值是否真实相异需真实行情库验证。详见各 lane
REPORT.md"待验证"节。

## 优化分析（均有测量数据，详见各 lane REPORT）

- engine.py 每日快照 deepcopy 占 20.6%（cProfile 250 日×300 标的 3.9s；手工重建
  Holding A/B 快 11.2×）；bars_to_dict 21.6%、write_daily 23%；撮合本体 <3%。
- stats round_trips iterrows 占 93%，to_dict(records) 对照 4.5~5.0×。
- cross_validate 检查 3 `groupby.filter(lambda)` 10 万笔 5.13s（占 84%），向量化
  参照 0.021s（244×）。
- 广度面板 warmup 28.8× 过配给；coerce 逐节点重扫 object 列约 200ms/次
  （factor-layer，与在册 F-FAC-07/ARCH-PERF-01 同域新测量）。
- strategy-layer：DUP-08b 缓存未覆盖标准路径；每日 dict 往返约 26ms/日。

## 未验证范围

真实行情/结果库全链路端到端、stats 之外的 labels 口径、live 回放（排除域）、
系统级未来扰动重跑；exploring/archive 策略仅抽样；factors/library.yaml 非全量
逐因子重验（F-FD* 波次在册为主，本轮仅查新问题）。本审查为子代理代码级核查 +
主控复跑验证，不替代真实券商对账层证据。

## 修复验收（2026-10-24 同日，用户授权直接修）

用户授权跳过冷却等待直接修复：仅修 4 条 P1（协议口径：用户要求修复时只修确认
P0/P1）；24 条 P2/P3 留 backlog 待处置。每条先建 RED 回归再最小修改：

| ID | 修复 | 回归与验收 |
| --- | --- | --- |
| F-CST-01 | `slippage.py` 改 Decimal(repr)+ROUND_HALF_UP（与 limits 同口径） | `tests/test_slippage.py` 新增 dyadic 半分位断言（8.625→8.63），保留原有 grid 断言；match/costs/limits 46 例不变 |
| F-STA-02 | `_compute_management_complexity` 改收 `exec_trades`；`cost_per_trade` 分母只数 BUY/SELL | `tests/test_stats.py` 新增 STK_DIV/ADJUST 行排除断言；探针复验 0.6667/6.855 与独立期望一致 |
| F-ATT-01 | 回放前按 `TRADE_EVENT_PRIORITY` 稳定排序（公司行为先于成交） | `tests/test_attribution.py` 两例（SELL 复活/BUY 覆盖）；真实引擎 fixtures 探针：除权日持股 23660→0、次日→24800 |
| F-RSR-07 | library.yaml 新增 `bench_ret`（expr: idx_ret）；value_momentum 以 materialize_only 声明；on_tick 首日 NaN 按 0 | `tests/test_value_momentum_benchmark.py` 四例：plan needs[index]、评分不混入、引擎 e2e bars 携带真实基准日收益、NaN 防污染 |
| F-MRD-03（用户点名扩大） | `validate_trades` 统一 exec_trades 口径，总成交/日均只数 BUY/SELL | `tests/test_cross_validate.py` 新增 notes 文案断言（HEAD 版 RED→修复 GREEN）；探针场景 B 不再误报 HIGH_FREQ |

真实库端到端首跑（value_momentum，20240101-20240630，真实行情库只读，未写真
结果库）：117 交易日、+10.29%、81 笔；决策窗口 30/30 非零 idx_ret，
_bull_mode 与 30 日基准累计 -4.98% 同向；avg_trades_per_day=81/117 与
F-STA-02 口径精确一致；benchmark_compare.strategy_total_return 与
total_return 同口径对账相等。

全量验收：`review_gate done --full` → anticorrupt/skill_sync/pytest 全 PASS
（902 例），Ruff 全绿；4 条 P1 已 `close`（修复摘要写入 backlog 条目）。

数值口径影响（旧结果是否重跑）：F-CST-01 仅 dyadic 半分位成交价差 1 tick，
无需强制重跑；F-STA-02 旧 stats_json 的 management_complexity/cost_per_trade
需重算；F-ATT-01 含除权日同日成交的历史归因需重跑；F-RSR-07 value_momentum
全部历史回测作废需重跑（趋势感知首次真正生效），且无 benchmark 的后端从静默
全 0 变为加载期 fail-fast。均已在 backlog 关闭条目中留档。

未验证范围（修复侧）：F-CST-01 上游触发价浮点表示的十进制意图偏差
（9.595 类）未在本轮处理（需 ConditionBuilder Decimal 化，另行评估）；
真实券商对账层证据未覆盖（本审查为代码级+复现+真实行情库验证）。

## backlog 登记状态

28 条发现已经 `review_gate.py check --scope diff` 登记入 backlog（full 的
14 天冷却不阻断 diff 登记；`last_full_audit` 未更新）；P1×4 与 F-MRD-03
（用户点名）修复后 close，open 余 23 条 P2/P3 待后续处置。修复前的 full
登记尝试（冷却 exit 2）：

`python scripts/review_gate.py check docs/reviews/2026-10-24/review_findings_20261024.yaml
--scope full` 实际输出（2026-10-24）：

```text
ERROR: 全量审查冷却期未过（上次 2026-10-12）；请用 --scope diff 验收改动
EXIT=2
```

按协议 exit 2 不更新 backlog。28 条发现以本记录 +
findings yaml 留档，待冷却期后（≥2026-10-26）或用户明确指示时再行登记；
P1×4 在登记后属门禁阻塞项，修复需用户另行授权（审查请求不等于修复授权）。
