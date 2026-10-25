# 因子研究与实验方法

定义/算子见[因子指南](factor_library.md)，参数见 [factor_eval 命令](cli_and_research.md#factor-eval)。
本文说明如何让因子评估、策略回测和研究结论使用可比口径。

[评估](#evaluation) · [Python API](#api) · [合成](#composite) · [实验](#experiments) · [研究状态](#state)

<a id="evaluation"></a>
## 先固定研究口径

记录因子定义版本、后端/数据窗口、候选池、缺失率、价格与前瞻收益口径。
先查本机因子库和后端实际字段；不要从旧总结推断当前有多少因子、哪类信号有效。
新增因子可放策略局部库，评估时显式 --factor-library，避免污染已有研究基线。

```bash
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630 \
  --universe CSI300 --forward 5 --exec-price next-open
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630 \
  --universe CSI300 --decay 1,3,5,10 --exec-price next-open
```

- close 口径是 T 收盘到 T+h 收盘；next-open 是 T+1 开盘到 T+h 收盘。用于开盘执行
  策略时复核后者，但它仍不模拟费用、涨跌停、仓位与未成交，不等于完整可实现收益。
- CLI 自动前伸 warmup；直接 compute_factors 不会取数或附着伪列，需调用方准备。
- --universe 用区间成分快照并集加载，再按 PIT 过滤评估样本；因子计算域、交易候选域、
  成分时点三者分别核对。引擎不是所有截面都先做相同的逐日 PIT 裁切。
- CLI 对显式坍缩因子输出日频标量，跳过截面 IC/分层/相关性；均值型全市场标量看时序，
  group_mean 保留行业面板时可以存在组间变异，不能沿用标量结论。
- 当前 CLI 对普通因子嵌套引用坍缩节点会拒绝；模型的特征路径与限制另查 ML 指南。

分析 IC 均值、RankIC、IR、有效日期/样本数、方向稳定性和分层单调性，不只挑最大值。
Q1 是最低因子值档；ascending=true 表示策略选择小值。数据不足的 NaN/“—”不是零效果。
重叠 h 日前瞻的分层累计曲线是研究统计，不是独立持仓回测的净值。

<a id="api"></a>
## Python 评估 API

输入使用同索引的 `(trade_date, symbol)` 面板；基础指标函数不读数据库，
`research.factor_eval.run_eval` 则是可注入 backend 的评估编排。

```python
from research.factor_eval import calc_ic, summarize_ic, calc_layered_returns

close = bars_df["close_hfq"]
horizon = 5
fwd = close.groupby("symbol").shift(-horizon) / close - 1
ic, rank_ic = calc_ic(factor_df["mom20"], fwd)
summary = summarize_ic(rank_ic)
layers = calc_layered_returns(factor_df["mom20"], fwd, n_quantiles=5)
```

必须按 symbol 分组 shift，不能在 date-major 面板上用扁平 pct_change().shift()
把其他标的收益错配过来。尾部无完整前瞻窗口的样本保留缺失，不补零。

| API | 输出与边界 |
| --- | --- |
| calc_ic(values, forward_returns, date_col="trade_date") | 每日 Pearson、Spearman Series；有效样本不足 3 则 NaN |
| summarize_ic(ic) | ic_mean/ic_std/icir/ic_positive_ratio/n_days；无有效观测时统计量 NaN、n_days=0 |
| calc_layered_returns(values, forward_returns, n_quantiles=5, date_col="trade_date") | 档号→累计曲线；分位边界重复会减少档数 |
| calc_factor_corr(factor_df, date_col="trade_date") | 每日截面 Pearson 相关的均值；不足两列为空，缺有效日为 NaN |
| calc_ic_decay(values, close_hfq, horizons, date_col="trade_date") | horizon 索引表；ic_mean/ic_ir/ic_win/rank_ic_mean/rank_ic_ir/rank_ic_win/n_days；horizons 需为正整数天数（非正数 ValueError） |

<a id="composite"></a>
## 多因子合成

```python
from research.composite import combine_factors, evaluate_composite

composite = combine_factors(
    factor_df, fwd, method="icir", window=60, min_periods=None, horizon=5,
)
evaluation = evaluate_composite(composite, fwd, n_quantiles=10)
```

| method | 权重 |
| --- | --- |
| equal | 每因子截面 zscore 后等权；无需历史 IC 窗口 |
| ic | 滚动 IC 均值，保留符号 |
| icir | 滚动 IC 均值/标准差，保留符号 |

IC 权重按绝对值归一化，rolling 后 shift(max(1,horizon))，t 日最多用到 t−horizon 日
信号已实现的收益。horizon 默认 1，必须等于实际前瞻期；5 日收益配默认值会引入前视。
min_periods 默认 max(2,window//2)，未满足历史条件时得分 NaN；部分权重缺失补零，
所有权重不可估计时保持 NaN。返回 Series，evaluate_composite 返回 ic/rank_ic 汇总及 layered。

此函数产生每日变化的合成得分，不会自动写成 YAML 静态权重；策略 eval_factor_specs
使用百分位排名而非 zscore。迁入策略须明确如何消费这些得分，并对齐实时可得的权重，
不能把样本全期拟合的最优权重当作当时已知。

<a id="experiments"></a>
## 有边界的实验

1. 固定对照：代码 commit + diff、策略/因子/模型版本、数据路径和覆盖、资金/费用、窗口。
2. 写一个可证伪的问题；优先单变量。机制整体替换可作一组处理，但须说明不能分离内部贡献。
3. 先用合适短窗验证行为和信号覆盖，再用预留样本外/跨市场状态窗口验证稳健性。
4. 有希望时测试相邻参数、极端边界或反向假设，寻找参数高原；不要无限搜索最高收益点。
5. 用交易明细解释收益/磨损/容量，结论区分样本内、样本外与待验证。窗口变长不自动等于样本外。

run 必须显式 --out 才能留结果库；同一 DuckDB 串行写，独立并行任务使用不同库。
扫描点路径以 base YAML 实际结构为准，见[sweep](cli_and_research.md#sweep)。
是否继续探索由用户授权、目标、资源预算和新增信息决定；单次评估完成后应交付，
不自动改为无限研究，也不要求每轮必须改代码。

市场状态描述可用指数首末收益、行业分化、因子分窗 IC；窗口收益用首末配对，不能用
MAX/MIN 代替。阈值和方向是实验假设，不把历史研究的数值写成所有任务的前置门槛。

<a id="state"></a>
## 跨轮研究状态

持续研究时读写 `results/research_state.yaml`（本机资产）。沿用已有格式与路径，不为
统一模板覆盖已有结论；没有持续研究需求时无需新建状态文件。

```yaml
baseline:
  code: "commit + diff 标识"
  config: "策略配置路径及版本"
  data: "后端、数据覆盖与快照标识"
  result: {db: "results/baseline.duckdb", run_id: 1}
directions:
  - name: "假设名称"
    status: exploring  # unexplored / exploring / completed / exhausted
    finding: "限定窗口与口径的结论"
current_direction: "假设名称"
next_plan: "可执行的一步，或任务已完成"
round_log:
  - "R1: 单变量、对照 db/run_id、实验 db/run_id、关键指标差异、结论"
```

状态丢失时从 runs/config_json/stats_json 与存盘配置恢复；created_at 不能证明代码版本，
config_json 也不是整个策略源码/因子库快照。无法重建的参数标未知。代码/数据变化后先解释
结果可比性，必要时重跑受影响基线；旧实验相对排序也可能改变，不能一概沿用。
