---
name: ddup-experiment-design
description: ddup 回测实验设计与执行：单变量/极端值/反向假设三原则、run.py 执行事实（--out 缺省不落盘、并行写锁）、sweep.py 网格语法与串行机制、两阶段验证防过拟合、结果判读纪律。设计实验、参数扫描、批量回测、验证过拟合时使用。
---

# ddup 实验设计与执行

假设 → 最小实验 → 证据。每轮研究只回答一个问题。CLI 全集见 `docs/cli_and_research.md`。

## 实验三原则

1. **单变量**：每轮只改一个东西（止损 0.08→无止损 ✓；同时改止损+频率+top_k ✗）
2. **极端值**：找到有效参数后测极端确认方向（止损 8% → 5%/15%/无；top_k=4 → 2 和 8；月频 → 周频和日频）
3. **反向假设**：有效设计必须测反面（"低波好"→高波组合显著更差？"不止损好"→加止损更差？一个指数池好→换其他指数池？）

## 工具选择

| 场景 | 工具 |
|---|---|
| 新想法是否有效 | run.py 单点 |
| 方向已有效找最优参数 / 敏感性 | sweep.py 网格 |
| 引擎/因子机制改动后回归 | `pytest tests/ -q`（引擎改动另跑 `python scripts/check_anticorrupt.py`） |

## run.py 执行事实（易踩）

```bash
python scripts/run.py strategies/.../config.yaml --start 20240101 --end 20250630 \
    --out results/exploring_r3.db [--capital 200000] [--no-report] [--report [path]]
```
- **`--out` 不显式给 → :memory: 不落盘，结果蒸发**（研究必须显式 --out）
- 报告 auto 路径 = `<策略目录>/reports/<yaml名>_<起>_<止>.html`；迭代期 `--no-report` 加速，最终阶段补：`python scripts/report.py <db> --out r3.html`（`--run-id` 缺省最新）
- 同一 db 多次 run 按 run_id 累积（多 run 结果库）
- **同一 db 文件禁止并行写**（sqlite 无 WAL/busy_timeout，开连接即写，并行必 SQLITE_BUSY）：多个独立策略并行跑必须各自不同 --out；同库累积只能串行

## sweep.py 网格

```yaml
# sweep.yaml
base: strategies/my_strategy/config.yaml
params:
  config.top_k: [2, 3, 4, 6, 8]
  config.conditions.stop_loss_pct: [0.05, 0.08, 0.12]
  config.rebalance_interval: [1, 5, 22]
```
```bash
python scripts/sweep.py sweep.yaml --start 20240101 --end 20250630 --out results/sweep_r3.db [--dry-run]
```
- params 点路径覆写 base，值列表做**笛卡尔积**（3×3×3=27 组）；`--dry-run` 预览组合
- 执行 = subprocess **串行**调 run.py（--no-report，共享 --out，规避并行锁）；失败组合打印 FAIL 跳过
- 结果写同库 runs + sweep_results 表（label/params/stats），末尾打印收益/Sharpe/MDD 汇总表
- 多 run 横向对比：`python scripts/compare.py results/sweep_r3.db --html cmp.html`（11 项指标表 + 归一化净值叠加）

## 两阶段验证（防过拟合）

```
阶段 1 快速筛选：近 ~18 个月
  通过标准：收益 > 基准 或 Sharpe > 0
阶段 2 长周期确认：~3.5 年以上窗口
  仅对阶段 1 通过者运行；短期好+长期垮 = 过拟合，标记并回溯哪段失效
```

## 结果判读纪律

- 单点/网格数字只是入场券：通过阶段 1 后必须深度分析（ddup-backtest-analysis）再定论
- sweep 最优组合警惕"网格噪声冠军"：最优与次优差距 < 噪声水平时选参数高原而非尖峰（相邻参数组都好的区域）
- 参数敏感性本身就是结论：sharpe 对 top_k∈[3,6] 平坦 = 策略稳健；只在 top_k=4 好 = 疑似过拟合
