---
name: ddup-research-loop
description: ddup 量化策略自主研究的元流程：多轮探索循环、方向管理与枯竭判断、research_state 状态文件、每轮自检清单、上下文管理。开始/继续任何策略研究任务、决定下一轮做什么、判断方向是否枯竭时必读——其余 5 个研究 skill 由本 skill 路由。
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
| API 细节速查 | `docs/index.md` 能力全景表 → 对应文档章节（按需切片读，不通读） |

## 每轮循环

```
Round N:
 1. 观察：读 results/research_state.yaml + 上轮一行摘要
 2. 假设："改 X 应改善 Y"（单变量，见 ddup-experiment-design）
 3. 实现：新建/修改策略或 sweep 配置（ddup-strategy-craft）
 4. 验证：run.py 单点 或 sweep.py 网格（必须显式 --out 落盘）
 5. 记录：state 文件追加一行摘要 + 更新方向状态

 每 2-3 轮：深度分析（ddup-backtest-analysis：trade_log SQL + cross_validate）
 每 5 轮或发现异常：debug 模式重跑 + replay.py 回放
 阶段 1 通过的方向：跑长周期确认（多年窗口）再定论
```

行为准则：每轮结束自动进入下一轮，不问用户；停止条件仅 ①方向全部 exhausted ②连续 3 轮无任何改进；每轮必须有新代码或新配置，禁止只分析不行动；变差不停——变差也是信息（验证了一个反面假设）。

## 状态文件 results/research_state.yaml（唯一跨轮记忆）

状态活在盘上，不在 context 里。每轮**开始读、结束写**；上下文重置/新会话从它恢复，零记忆依赖。

```yaml
directions:
  - name: "方向A"               # 示意格式
    status: exhausted           # unexplored | exploring | exhausted
    rounds: 3
    best: "+12.3% / sharpe 0.9 / dd -15.0% (results/exploring_r3.db run_id=2)"
    finding: "一句话关键结论"
current_direction: "方向B"
next_plan: "下一轮要做的具体一件事"
round_log:
  - "R3: strategy_v2 改单一变量X ret +5.1%→+12.3%（改进，一句话结论）"
```

摘要格式（一行）：`R{N} {策略}: ret= sharpe= dd= trades= | 关键trigger盈亏 | 结论:下一步`。

## 方向管理

- 枯竭判断：连续 2 轮同一方向无改进 → exhausted → 切换，不恋战
- 新方向来源（按优先级）：
  1. 未用的数据大类（先读 `factors/library.yaml` 现状名录找未用类别，见 ddup-factor-research）
  2. 未用的策略机制（条件买入、target_value、自定义 handler、坍缩因子门控、model_exit）
  3. 已有结论的反面（动量↔反转、集中↔分散、指数池↔指数池、止损↔不止损）
  4. 双序列算子（`corr`/`beta`/`resid_std` = 价量相关/贝塔/特质波动，独立因子类别）
  5. 策略级别升级（L0→L4，见 ddup-strategy-craft；禁跳级）
  6. 线性组合 IC 饱和后叠加 ML（ddup-ml-research）

## 上下文管理

- 不复制完整回测输出进 context；只抽一行摘要 + 关键数字
- 批量数据提取（trade_log 大表 SQL）、独立策略实现 → 派子 agent，主线只做决策
- context 将尽：先把 state 文件写全，再开新会话从 state 恢复

## 每轮自检清单

- [ ] 本轮改的是哪个单一变量？
- [ ] 结果落盘到哪个 db？（run.py 不显式 `--out` 则 :memory: 蒸发）
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
- 手工建 N 个 config_vN.yaml（用 sweep.py）
- L0 直接跳 L4
