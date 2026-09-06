---
name: ddup-research-loop
description: ddup 量化策略自主研究的元流程：多轮探索循环、方向管理与枯竭判断、research_state 状态文件、每轮自检清单、上下文管理。开始/继续任何策略研究任务、决定下一轮做什么、判断方向是否枯竭时必读——其余研究/实盘 skill 由本 skill 路由。
---

# ddup 研究主循环（元 SOP）

策略研究是多轮探索，不是单次任务。本 skill 是入口：每轮开始读它，按其路由加载对应专题 skill。

## 路由表（何时加载哪个 skill）

| 要做什么 | 加载 |
|---|---|
| 设计/评估因子、IC、分层、多因子合成 | `ddup-factor-research` |
| 创建/修改策略、条件单、target_value、自定义 handler | `ddup-strategy-craft` |
| 设计实验、参数扫描（sweep）、过拟合验证 | `ddup-experiment-design` |
| 分析回测结果、诊断亏损、归因、回放 | `ddup-backtest-analysis` |
| 训练/消费 ML 模型、ml_ 列、model_exit | `ddup-ml-research` |
| 实盘部署：账本/每日操作单（研究收官） | `ddup-live-ops` |
| API 细节速查 | `docs/index.md` 能力全景表 → 对应文档章节（按需切片读，不通读） |

## 第 0 步：刻画市场状态（设计策略前，凭感觉定方向=禁止）

用行情库三个实测指标回答"当前环境适合什么策略"（数据表：`idx_factor_pro` 指数日频、`sw_daily` 申万行业日线）：

1. **大小盘相对强弱**（决定 universe）：半年窗口 idx_factor_pro 首/末 close **配对相除**（MAX/MIN 是窗口极值不是收益，必须首末配对）；CSI500 vs CSI300 差 >8pp → 中盘牛（500 池 beta 顺风），CSI300 领先 → 大盘/价值市（500 池被 beta 拖累）
2. **行业分化度**（决定行业动量可用性）：sw_daily 各行业窗口收益的 P90−P10 分化（sqlite3 无 STDDEV，用 Python statistics）；经验阈值：>45pp → 行业轮动市（industry_mom 有效），~25-33pp → 无区分度只剩换手损耗
3. **前瞻 IC**（决定因子权重与持有期）：`python scripts/factor_eval.py "<候选因子>" --start <近6月> --end <今日> --decay 5,10 --universe <候选池>`；10d RankIC > 0.04 = 核心因子；价值因子 IC 转负 ≠ 权重归零（组合分散器，仅降权）

输出格式：`当前环境 = X 型市场，适合 Y 机制，不适配 Z` + 三个指标数字，写进 research_state.yaml 再进下一轮。已证反例（勿重试）：universe 单换 300→500 只加 beta 不加 alpha；固定双周持有零离场全窗口差（TREND_BREAK 快切才是 alpha 引擎）。

## 每轮循环

```
Round N:
 1. 观察：读 results/research_state.yaml + 上轮一行摘要
 2. 假设："改 X 应改善 Y"（单变量，见 ddup-experiment-design）
 3. 实现：新建/修改策略或 sweep 配置（ddup-strategy-craft）
 4. 验证：run.py 单点（必须显式 --out，否则 :memory: 蒸发）或 sweep.py 网格（缺省落盘 cwd/sweep_result.db，建议显式命名）
 5. 记录：state 文件追加一行摘要 + 更新方向状态

 每 2-3 轮：深度分析（ddup-backtest-analysis：trade_log SQL + cross_validate）
 每 5 轮或发现异常：debug 模式重跑 + replay.py 回放
 阶段 1 通过的方向：跑长周期确认（多年窗口）再定论
```

行为准则：每轮结束自动进入下一轮，不问用户；停止条件仅 ①所有方向 exhausted 或 completed 且 next_plan 无可执行项 ②连续 3 轮空转（无信息价值）；每轮必须有新代码或新配置，禁止只分析不行动；变差不停——变差也是信息（验证了一个反面假设）。**"无改进" = 无信息价值的空转轮（无假设/无新结论/纯重复）；失败但有结论的轮不计入**（实证：R19-R22 连续 4 轮排除后 R23 才成功，按字面计数会过早停掉）。

## 状态文件 results/research_state.yaml（唯一跨轮记忆）

状态活在盘上，不在 context 里。每轮**开始读、结束写**；上下文重置/新会话从它恢复，零记忆依赖。

```yaml
directions:
  - name: "方向A"               # 示意格式
    status: exhausted           # unexplored | exploring | completed | exhausted
    rounds: 3
    best: "+12.3% / sharpe 0.9 / dd -15.0% (results/exploring_r3.db run_id=2)"
    finding: "一句话关键结论"
current_direction: "方向B"
next_plan: "下一轮要做的具体一件事"
round_log:
  - "R3: strategy_v2 改单一变量X ret +5.1%→+12.3%（改进，一句话结论）"
```

摘要格式（一行）：`R{N} {策略}: ret= sharpe= dd= trades= | 关键trigger盈亏 | 结论:下一步`。
**round_log 与 best 必须带 (db 文件, run_id)**——否则 state 丢失后无法从结果库追溯该轮；跨引擎世代比较先归因（见下"状态恢复"）。

## 状态恢复（research_state.yaml 丢失/上下文重置时）

禁止凭空续写，按此重建：

1. **从 results/*.db 恢复轮次统计**：runs 表（run_id/strategy/start/end/status/stats_json→total_return/sharpe/max_drawdown）；命名约定 `r{N}_{实验名}_{窗口}.db` 按前缀归组重建方向；config_json 通常为空 dict，参数只能从 stats 反推，别假设完整
2. **先核对引擎世代**（先于一切结论）：runs.created_at vs `git log` 提交时间——**旧引擎结果不可直接对比当前引擎**（如 2026-08 ML 子系统重构 `_ml_config`/`MLEvaluator` → models 节+meta v3，旧 run 的分数口径全变）；旧 run 只作方向线索，必须重跑当前引擎建新基线
3. **存盘策略可加载性**：`load_strategy("strategies/selected/xxx/config.yaml")` 抛错=失效；加载成功 ≠ 行为正确（旧 `type: ml_feature` 被静默当普通评分因子；materialize_only 趋势因子丢失 → TREND_BREAK 永不触发）——回测前先跑一个窗口看 trade_log trigger 分布

恢复后自检：摘要来自 DB 实证（非记忆）；state 记录引擎世代分界（"R1-R4 旧引擎，R5 起新引擎"）；结论标注样本内/样本外。

## 方向管理

- 枯竭判断：同一方向连续多轮无改进（实测经验 ~6 轮；按"无信息价值"定义计数）→ exhausted → 切换，不恋战
- 新方向来源（按优先级）：
  1. 未用的数据大类（先读 `factors/library.yaml` 现状名录找未用类别，见 ddup-factor-research）
  2. 未用的策略机制（条件买入、target_value、自定义 handler、坍缩因子门控、model_exit）
  3. 已有结论的反面（动量↔反转、集中↔分散、指数池↔其他指数池/全市场、止损↔不止损）
  4. 双序列算子（`corr`/`beta`/`resid_std` = 价量相关/贝塔/特质波动，独立因子类别）
  5. 策略级别升级（L0→L4，见 ddup-strategy-craft；禁跳级）
  6. 线性组合 IC 饱和后叠加 ML（ddup-ml-research）

## 上下文管理

- 不复制完整回测输出进 context；只抽一行摘要 + 关键数字
- 批量数据提取（trade_log 大表 SQL）、独立策略实现 → 派子 agent，主线只做决策
- context 将尽：先把 state 文件写全，再开新会话从 state 恢复

## 每轮自检清单（实现前门禁，不是事后确认）

**写代码/改配置之前先过一遍，违者本轮无效：**

- [ ] 本轮改的是哪个单一变量？新参数只允许一个（多参数实验拆多轮；扫描多个取值算同一变量）
- [ ] 是否引入了新机制而非调参？（新机制默认多变量——第一轮先把机制本身跑到可比基线，再谈参数）
- [ ] 结果计划落盘到哪个 db？（run.py 不显式 `--out` 则 :memory: 蒸发）
- [ ] 对照组是什么？（单点跑完必须与上一轮同窗口数字直接对比，先想好对比表）

**跑完后确认：**

- [ ] 一行摘要 + 与上轮比改进/退步？
- [ ] 退步是否验证了有意义的反面假设？
- [ ] 到深度分析节奏了吗（每 2-3 轮）？
- [ ] 当前方向连续几轮无改进？该标 exhausted 吗？
- [ ] 策略当前 L 几？该升级吗（先理解当前级行为再升）？
- [ ] state 文件已更新？

## 探索层禁令

- 只看 total_return/sharpe 下结论（必须钻 trade_log）
- 同时改多个参数
- 只跑短期不做长周期确认
- 发现一个好策略就停止探索
- 不记录就进下一轮
- 手工建 N 个 config_vN.yaml（用 sweep.py；列表型参数走整数下标路径，如 `config.factor_specs.0.weight`，见 ddup-experiment-design）
- L0 直接跳 L4
- 引擎修复/重构后不重跑基线就对比旧数字（先归因——ddup-backtest-analysis §6）
