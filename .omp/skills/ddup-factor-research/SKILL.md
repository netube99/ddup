---
name: ddup-factor-research
description: ddup 因子研究全流程：数据资产地图（契约/派生/伪列与后端扩展字段）、因子 YAML 定义规则、21 算子加工梯度、factor_eval 评估、多因子合成、坍缩因子 compute_breadth 口径与四大陷阱。设计新因子、评估 IC/分层/衰减、多因子合成、诊断 IC 失真时使用。
---

# ddup 因子研究

因子是纯 YAML 数据（`factors/library.yaml`），引擎只提供表达式机制。API 细节见 `docs/factor_library.md`（按需切片读）。

## 研究流水线（严格按序）

```
1. 查名录：通读 factors/library.yaml 现状（分类注释/命名），避免重造、找灵感
   （仓库只分发模板 factors/library.yaml.template；实际名录是本地资产，以文件为准）
2. 写表达式：library.yaml 登记（格式见下；自定义因子库可参考 strategies/examples/multi_model/factors.yaml）
3. 验证：pytest tests/ -q（因子库/物化机制由测试全量覆盖，改 library.yaml 后必跑）
4. 评估：factor_eval.py 测 IC/分层/衰减（下文）
5. 合成：多因子用 research.combine_factors 找权重（不直接手填 YAML 权重）
6. 入策略：factor_specs 引用（ddup-strategy-craft）
```

## 数据资产地图（因子灵感来源）

- **契约列**（9，必有）：`open` `high` `low` `close` `vol`(手) `adj_factor` `pre_close` `up_limit` `down_limit`
- **派生列**（引擎自动）：`open_hfq` `high_hfq` `low_hfq` `close_hfq`（裸价×adj_factor）、`pct_chg`
- **伪列**（引擎按需附着）：`industry`、`log_mktcap`（=log(total_mv)，自动补请求 total_mv）、`idx_ret`（需 benchmark）
- **扩展字段**：以 `adapters/` 后端表单 `extra_fields` 登记为准（未登记列运行期拒绝）——日频基本面/资金流/筹码/两融/涨跌停等数据类皆走此通道；**先做因子前先看后端登记了什么**
- 排名/因子一律用 hfq 口径（表达式里写 `close_hfq` 等），裸价排名会因除权跳空产生假信号

## 因子 YAML 格式（load_library 校验）

```yaml
factors:
  my_factor:
    expr: "ma(close_hfq, 20) / close_hfq - 1"   # 必填；纯表达式或算子表达式；可引用其他因子名（DAG，成环报错）
    where: "pct_chg > -0.095"                   # 可选；后置掩码置 NaN 不删行；也支持算子表达式（与 expr 同规则）
    description: "..."                          # 可选，加载器忽略
```
规则：算子参数必须位置传、尾部标量必须数字常量；禁 `&` `|` `not`（纯表达式路径支持 and/or）；因子名禁撞保留字（契约/派生/伪列名 + `abs` `log`）；YAML 重复键 fail-fast。

## 21 算子梯度（btcore/factors/ops.py 白名单）

| 层 | 算子 |
|---|---|
| TS 时序（按 symbol 因果滚动） | `delay` `delta` `roc` `ma` `ema` `std` `sum` `max` `min` |
| TS 双序列（独立因子类别，勿忽略） | `corr`(x,y,n) 价量相关 / `beta`(x,y,n) / `resid_std`(x,y,n) 特质波动 |
| 截面保形（按日） | `abs` `log` `rank`(百分位∈(0,1]) `zscore` `winsorize`(x,p) |
| 分组 | `group_rank`(x,g) 组内百分位 / `neutralize`(x,g,size) 行业+市值 OLS 残差 |
| 坍缩（同日广播） | `mean`(x) 全市场标量 / `group_mean`(x,g) 组均值 map 回个股 |

设计原则：每次只加一层变换，每层用 factor_eval 验证 IC 变化；先验证裸列有无 alpha，不要起手堆满算子。

## factor_eval.py 评估（CLI，纯终端无落盘）

```bash
python scripts/factor_eval.py mom20,vol_z --start 20240101 --end 20250630 \
    [--universe CSI500] [--forward 5 | --decay 1,3,5,10] [--n-quantiles 5] \
    [--model path.onnx] [--benchmark 000300.SH]
```
- 输出三段：IC 汇总（Pearson/RankIC + IR + 胜率 + n_days）、分层回测（**Q1=最低档**，含多空 Qmax−Qmin）、≥2 因子加相关性矩阵
- `--universe`：指数别名 → 成分快照回溯并集 + PIT 逐日过滤；**研究股票池必须与策略候选池一致**
- `--decay` 与显式 `--forward`≠5 互斥；`--model` 仅 panel scope（ddup-ml-research）；坍缩因子自动走全市场 compute_breadth
- 失败均 stderr+退出码 1（未知因子/无成分/窗口无数据/嵌套坍缩…）
- warmup 自动前伸：start − max(365, int(最大窗口×1.5)+10) 日历天（引擎同源）

程序化 API（research/factor_eval.py，纯函数）：`calc_ic` / `summarize_ic` / `calc_layered_returns` / `calc_factor_corr` / `calc_ic_decay`。输入均为 (trade_date, symbol) MultiIndex 面板。

## 多因子合成（research/composite.py）

```python
comp = combine_factors(factor_df, fwd_ret, method="icir", window=60)  # equal|ic|icir
ev = evaluate_composite(comp, fwd_ret)   # → {"ic":…, "rank_ic":…, "layered":…}
```
前视保护与引擎一致：t 日权重仅用 ≤t-1 日 IC（rolling 后 shift(1)），前 ~window 日 NaN；每因子先截面 zscore，权重按 |w| 归一化。对比三种 method 后，按最优设定策略 factor_specs 的 weight/ascending。

## 坍缩因子特例

- 研究侧 `compute_factors` 的聚合口径 = 传入 df 的股票池（窄池 ≠ 引擎全市场口径）；评估坍缩因子**必须**用 `compute_breadth(name, backend, lib, start, end, benchmark=...)`（全市场流式分块，与引擎同源口径；`lib` 为 load_library() 结果，`benchmark` 在因子引用 `idx_ret` 时必传）
- 坍缩因子同日全市场同值，无截面变异：不能算 IC/分层，改时序维度（与基准收益比对 / 择时门控信号）

## 四大陷阱（每轮因子工作前过一遍）

| # | 陷阱 | 症状 | 修复 |
|---|---|---|---|
| 1 | warmup 不足 | 前 N 天全 NaN，IC 失真 | 引擎/factor_eval 自动前伸 max(365, 窗口×1.5+10)；研究侧自行保证同等前伸 |
| 2 | 口径自负 | 全市场 IC 高，回测 IC 低 | 研究池=策略候选池（`--universe` 限定），不外推 |
| 3 | ascending 语义 | 排序方向反 | IC 正 → `ascending: false`（值大排前，默认）；IC 负 → true |
| 4 | 坍缩截面评估 | calc_ic 全 NaN | 改 `compute_breadth` + 时序评估 |
