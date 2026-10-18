# 因子定义与物化

因子是纯 YAML 数据，策略与研究按名消费。默认库为本机 `factors/library.yaml`，
仓库分发 `factors/library.yaml.template`；策略可用 `factor_library: factors.yaml`
引用相对于策略 YAML 的本地库。具体因子不内置在引擎中，也没有因子运行时注册机制。

按需阅读：[定义](#definition) · [表达式](#expressions) · [算子](#operators) ·
[数据列](#columns) · [DAG 与窗口](#dag) · [物化域](#materialization) · [API](#api)。
评估 IC、分层和合成见[因子研究](factor_research.md)，策略评分见[策略接口](strategy_guide.md#factors)。

<a id="definition"></a>
## 定义与加载

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"
    description: "20 日后复权动量"
  vol20:
    expr: "std(roc(close_hfq, 1), 20)"
  quality:
    expr: "mom20 / (vol20 + 0.001)"
    where: "vol20 > 0"
```

`expr` 必填；`where` 是求值后的掩码，False/0/NaN 对应值置 NaN，不删除网格行；
`description` 供人阅读。where 也参与依赖闭包与窗口推导，但不参与 CSE 子树提取。

加载器拒绝重复 YAML 键、缺 expr、非法表达式、保留名和依赖环。
factor_specs 的入口名字不存在会在解析闭包时报错；表达式内部的未知名字可能是后端
扩展列，直到取数或求值才暴露。不能把“库可加载”当成“依赖数据齐全”。

禁止用作因子名的保留列/函数名：

```text
open high low close vol amount adj_factor pre_close up_limit down_limit
open_hfq high_hfq low_hfq close_hfq pct_chg idx_ret log_mktcap industry abs log
```

推荐使用小写蛇形名称，描述写明经济含义、单位、预期方向、数据可得时点。
已有因子删除前检查策略、自定义库、模型 meta 特征契约与研究记录，不能只查 Python 引用。

<a id="expressions"></a>
## 两条表达式路径

| 表达式 | 求值器 | 支持与限制 |
| --- | --- | --- |
| 无函数调用 | pandas.eval / numexpr | 逐行算术、比较、and/or、&/\|、not、链式比较 |
| 有函数调用 | 固定算子表 + AST 求值器 | 白名单调用、算术、比较、and/or；不支持 &/\|、not、链式比较 |

算术支持 `+ - * / ** % //`，一元 `+/-`；禁止属性、下标和外部函数调用。
用 `log(close_hfq)`，不能写 `np.log(...)`。算子按位置传参；窗口必须为正整数字面量，
winsorize 分位必须在 `(0, 0.5)`；不能用变量窗口、关键字参数或任意 Python 常量。

where 沿用 expr 的求值路径：纯 expr 搭配含算子的 where 会在加载时报错；
算子 expr 可配纯算术或算子 where，均遵守算子路径语法。

```yaml
factors:
  ep_ttm:
    expr: "1 / pe_ttm"
    where: "pe_ttm > 0"
  positive_days5:
    expr: "sum((pct_chg > 0) and (vol > 0), 5)"
```

<a id="boolean-score"></a>
### 布尔计分

纯表达式中的布尔加法可能按 OR 求值；计数先显式乘 1，不能假设 True + True = 2：

```yaml
factors:
  ema_5: {expr: "ema(close_hfq, 5)"}
  ema_20: {expr: "ema(close_hfq, 20)"}
  ema_60: {expr: "ema(close_hfq, 60)"}
  ema_250: {expr: "ema(close_hfq, 250)"}
  ema_bullish:
    expr: "(ema_5 > ema_20) * 1 + (ema_20 > ema_60) * 1 + (ema_60 > ema_250) * 1"
    description: "0–3 分"
```

算子路径比较结果为数值 0/1；布尔组合按 `!= 0` 转换，NaN 不能当作 False 的通用
替身。事件列缺失与事件未发生是否等价，须由数据定义决定；必要时在后端明确填充值。

<a id="operators"></a>
## 算子参考

输入输出均对齐 `(trade_date, symbol)` 网格；TS 按 symbol 滚动，截面操作按 trade_date。
`abs/log` 虽列入保形族，实际为逐元素变换。窗口开销是在子表达式所需行数上增加的行数。

| 算子 | 签名 | 语义 | 窗口开销 |
| --- | --- | --- | --- |
| `delay` | (x, n) | n 行前值 | n |
| `delta` | (x, n) | x − delay(x,n) | n |
| `roc` | (x, n) | x / delay(x,n) − 1，基数 0 → NaN | n |
| `ma` | (x, n) | 简单移动平均 | n−1 |
| `ema` | (x, n) | span=n，adjust=False | 3n−1 |
| `std` | (x, n) | 样本滚动标准差 | n−1 |
| `sum` | (x, n) | 滚动和，可计数 0/1 事件 | n−1 |
| `max` | (x, n) | 滚动最大值 | n−1 |
| `min` | (x, n) | 滚动最小值 | n−1 |
| `corr` | (x, y, n) | 滚动相关，分母 0 → NaN | n−1 |
| `beta` | (x, y, n) | x 对 y 含截距回归的斜率 | n−1 |
| `resid_std` | (x, y, n) | 回归残差总体标准差，负浮点残差方差截为 0 | n−1 |
| `abs` | (x) | 绝对值 | 0 |
| `log` | (x) | 自然对数 | 0 |
| `rank` | (x) | 当日百分位排名，(0,1] | 0 |
| `zscore` | (x) | 当日 (x−均值)/样本标准差，std=0 → NaN | 0 |
| `winsorize` | (x, p) | 截面裁剪到 p 与 1−p 分位 | 0 |
| `group_rank` | (x, g) | 当日组内百分位 | 0 |
| `neutralize` | (x, g, size) | 当日行业哑变量 + size + 常数 OLS 残差 | 0 |
| `mean` | (x) | 当日截面均值，广播回每行 | 0 |
| `group_mean` | (x, g) | 当日组均值，映射回组内每行 | 0 |

普通 rolling 窗口未满为 NaN；EMA 从已有观测开始，不采用同样的满窗门槛。
TS 窗口按标的已有行计数，缺行情行不自动补成交易日。
neutralize 有效样本数不超过设计矩阵列数时当日全 NaN；缺行业值的行并非自动剔除，
其行业哑变量为零。不要把缺分类当作经过了可靠行业中性化。

<a id="columns"></a>
## 数据列与量纲

| 来源 | 列与含义 | 供给方式 |
| --- | --- | --- |
| 契约 | open/high/low/close 裸价；vol（手）；adj_factor；pre_close；up_limit/down_limit | 后端必需列，详见后端指南 |
| 派生 | open_hfq/high_hfq/low_hfq/close_hfq = 裸价 × adj_factor | 引擎派生，不请求后端 |
| 派生 | pct_chg = (close−pre_close)/pre_close，分母 0 → NaN | 小数收益；pre_close 为交易所除权口径 |
| 伪列 | industry | 需 get_stock_industries；group_mean 规划也会请求 |
| 伪列 | log_mktcap = log(total_mv)，非正 → NaN | 规划自动补请求 total_mv |
| 伪列 | idx_ret：基准日收益按日期广播 | 需基准行情能力与 benchmark |
| 扩展 | amount、turnover_rate、pe_ttm、财报/资金流/筹码等 | 以实际后端 extra_fields 为准，不预设本机字段全集 |

价格趋势与排名使用 hfq；成交额、换手、股息率等保留各自量纲，不能全部乘复权因子。
`amount` 不是必需列，元/千元、total_mv 的万元等单位由后端定义，做比值前核对。
财报按公告可得日对齐，详见[后端数据对齐](backend_guide.md#alignment)。

<a id="dag"></a>
## DAG、窗口与评分分离

名称引用形成有向无环图，闭包按拓扑序计算。基础列窗口为 1，多分支取最大，
引用节点传递累加窗口；例如 `std(roc(close_hfq,1),20)` 需要 `1+1+19=21` 行。
EMA 无限记忆以 3n 行近似。主面板 warmup 为 `max(365, int(rows×1.5)+10)` 日历天；
广度面板采用其独立推导窗口。数据缺口仍可能导致有效历史不足。

`factor_specs` 只按名引用，不接受内联 expr。loader 挂接 FACTOR_NODES；
`materialize_only: true` 使因子可读取但不进入 `eval_factor_specs` 评分。
评分工具对每个因子做截面百分位，再按 weight 加权；ascending=false 为大值优先，
true 为小值优先。因子原始值、研究合成 zscore 和策略排名得分是三个不同对象。

<a id="materialization"></a>
## 计算域、坍缩与 CSE

| 路径 | 实际计算域 | 输出 |
| --- | --- | --- |
| 引擎主面板 | factor_universe；未声明则交易域加载并集 | 因子列；物化后裁到交易域 |
| 引擎广度面板 | 全市场、依赖窄列与独立 warmup | 坍缩节点投影回主面板 |
| compute_factors | 调用方传入 df 的范围 | 每个请求因子一列，无自动取数/伪列 |
| compute_breadth | 自动取全市场数据 | 每日一个标量；group 类型是各行业组值等权均值 |

`mean` 的同日值相同；`group_mean(x, industry)` 组内相同、组间可以不同。
引擎保留行业值映射；compute_breadth 把它压成全市场日频标量，两者输出形状与信息不同。
当前 factor_eval CLI 对识别出的两种坍缩类型都走标量路径、跳过截面评估，不能据此
断言所有行业均值面板都无截面变异。行业排序研究需保留完整分组面板并明确样本单位。

投影按原网格对齐并保留 where 掩码；group 键应使用 industry。含坍缩表达式再混入
组内可变项（例如 group_mean + zscore）不适合用“每组 first”压成标量，会丢信息。
被两路共同依赖的截面节点各在自己的域求值；不把窄池统计冒充全市场统计。

`compute_breadth` 按 chunk_days 查询，carry 保留自 warmup 起的全部原始行。
这能保留停牌前历史与滚动累加的浮点边界；内存随处理区间增长，不是 O(chunk)。
EMA 的无限记忆近似、不同数据起点和计算域仍是比较时必须固定的条件。

CSE 自动合并完全相同 `(expr, where)` 节点，并提取重复的非坍缩 Call 子树为临时节点；
where 不做子树重写，含坍缩调用的子树不提取。临时列物化后删除。
`validate_materialization` 返回 issues；缺列或过多 NaN 会告警，告警不等于自动补齐数据。

<a id="api"></a>
## Python 入口

```python
from btcore.factors.library import (
    load_library, resolve_spec, resolve_closure, compute_factors, compute_breadth,
)

lib = load_library("factors/library.yaml")
nodes = resolve_closure(["mom20"], lib)
values = compute_factors(["mom20"], bars_df, lib)
# bars_df 须自行准备历史、派生列与伪列
breadth = compute_breadth(
    "pct_above_ma20", backend, lib, "20240101", "20240630",
    benchmark="000300.SH", chunk_days=60,
)
```

| API | 返回/用途 |
| --- | --- |
| load_library(path=None) | 校验后的名称→定义；默认本机 factors/library.yaml |
| resolve_spec(spec, library=None) | 规范化 name/weight/ascending/materialize_only |
| resolve_closure(names, library=None) | 传递依赖闭包 |
| compute_factors(names, df, library=None) | 与 df 同索引 DataFrame；单因子也传列表 |
| compute_breadth(name, backend, lib, start, end, *, benchmark=None, chunk_days=60) | 日期索引 Series，自动 warmup；非坍缩入口报错 |
| plan.build_factor_plan / materialize | 引擎、训练共用供给计划与物化；实现细节查同名源码 |
| plan.ensure_pseudo_columns / derive_fields | 程序化准备派生/伪列，不另写一条特征管线 |

定义示例见[模板](../factors/library.yaml.template)和
[multi_model 本地库](../strategies/examples/multi_model/factors.yaml)。
