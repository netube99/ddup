# 因子库指南

因子定义集中在 `factors/library.yaml`，策略与研究两侧按名字消费同一份定义、同一条计算路径。纯数据驱动，无类、无运行时注册表、无全局可变状态。

---

## 快速开始

从模板复制 `library.yaml`：

```bash
cp factors/library.yaml.template factors/library.yaml
```

模板包含一组示例因子（动量/乖离率/换手率/价值/截面标准化/行业聚合/事件型），
可直接用于策略开发和学习。在此文件上添加、修改自有因子即可；
`library.yaml` 已在 `.gitignore` 中排除，不会被提交到版本库。

---

## 模型：面板上的计算 DAG

所有因子都是 `(trade_date, symbol)` 双索引面板上的计算节点。
每个节点有一条表达式 `expr`，可以是以下两种形态之一：

1. **逐行表达式**（纯列间算术/比较）：走 `pandas.eval` 安全子集，禁止函数调用和属性访问。
2. **算子表达式**：包含白名单算子调用（见下文算子表），支持自由嵌套和命名引用。

两种形态在一条表达式里可以混合嵌套；节点之间可以按名字相互引用，
形成有向无环图（DAG）。加载时系统自动做拓扑排序和环检测，
未知引用会立刻报错。

还有一个可选的 `where` 掩码：**求值之后**把不满足条件的值置为 NaN
（False → NaN），不删行、不破坏时间轴窗口。

### 最小示例

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"          # ts 算子：20 日动量

  value:
    expr: "dv_ttm / pb"                 # 逐行表达式
    where: "pb > 0"                     # 只看 PB 为正的股票

  industry_mom:
    expr: "group_mean(mom20, industry)"   # 引用 mom20，按行业聚合

  pct_above_ma20:
    expr: "mean(close_hfq >= ma(close_hfq, 20))"  # 嵌套：市场广度
```

---

## 因子名保留字

以下名字被系统占用，不能用来命名因子。违反时加载期直接报错
`ValueError: 因子名 'xxx' 与保留列名冲突`：

- bars 必需列：`open` `high` `low` `close` `vol` `amount` `adj_factor` `pre_close` `up_limit` `down_limit`
- 引擎派生列：`open_hfq` `high_hfq` `low_hfq` `close_hfq` `pct_chg`
- 伪列：`idx_ret` `log_mktcap` `industry`

---

## 算子表（共 20 个）

所有算子都是 **grid→grid** 变换：输入输出都对齐 `(trade_date, symbol)` 面板网格。
这是算子可以自由嵌套的根本原因。

### 标量族（逐元素变换，不消耗历史窗口）

| 算子 | 签名 | 说明 |
|---|---|---|
| `abs(x)` | 1 Series | 逐元素绝对值。NaN/NA 保持。 |

### ts 族（沿时间轴，per symbol 滑动窗口）

| 算子 | 签名 | 说明 |
|---|---|---|
| `delay(x, n)` | 1 Series + 1 标量 | 滞后 n 期。`n` 必须是正整数 |
| `delta(x, n)` | 1 Series + 1 标量 | n 期差分：`x - delay(x, n)` |
| `roc(x, n)` | 1 Series + 1 标量 | n 期变动率：`x / delay(x, n) - 1`。分母为 0 时结果为 NaN |
| `ma(x, n)` | 1 Series + 1 标量 | n 期简单移动平均。窗口参数必须 ≥ 1 |
| `ema(x, n)` | 1 Series + 1 标量 | 指数移动平均（`adjust=False`）。工程上窗口等效 3n |
| `std(x, n)` | 1 Series + 1 标量 | n 期滚动标准差 |
| `sum(x, n)` | 1 Series + 1 标量 | n 期滚动和。事件计数用这个：`sum(条件, n)` |
| `max(x, n)` | 1 Series + 1 标量 | n 期滚动最大值 |
| `min(x, n)` | 1 Series + 1 标量 | n 期滚动最小值 |
| `corr(x, y, n)` | 2 Series + 1 标量 | x 与 y 的 n 期滚动相关系数（闭式矩展开） |
| `beta(x, y, n)` | 2 Series + 1 标量 | x 对 y 的 n 期滚动回归斜率 |
| `resid_std(x, y, n)` | 2 Series + 1 标量 | x 对 y 回归残差的 n 期滚动标准差（特质波动率口径） |

回归类算子（corr / beta / resid_std）用闭式矩展开向量化实现，
分母为零的行结果为 NaN。所有窗口参数都必须是**数字常量**（不能是表达式）。

### xsec 保形族（逐日截面变换，输出形状与输入一致）

| 算子 | 签名 | 说明 |
|---|---|---|
| `rank(x)` | 1 Series | 逐日截面百分位排名（percentile rank），值 ∈ [0, 1] |
| `zscore(x)` | 1 Series | 逐日截面标准化：(x - 均值) / 标准差 |
| `winsorize(x, p)` | 1 Series + 1 标量 | 逐日截面缩尾：p ∈ (0, 0.5)，超出 p/(1-p) 分位的值截断 |
| `group_rank(x, group)` | 2 Series | 逐日行业内百分位排名 |
| `neutralize(x, group, size)` | 3 Series | 行业+市值中性化：逐日对行业哑变量 + size 做 OLS 取残差 |

保形 xsec 算子在**候选池并集**口径上逐日计算——这就是策略 `select()` 里
`eval_factor_specs` 看到的因子值口径。

### xsec 坍缩族（逐日全市场聚合，再投影回主面板）

| 算子 | 签名 | 说明 |
|---|---|---|
| `mean(x)` | 1 Series | 全市场均值，按日期广播到主面板每行 |
| `group_mean(x, group)` | 2 Series | 按 (日期, 分组) 聚合，按 (日期, 分组) map 回个股 |

坍缩算子在**全市场**面板上聚合（不限于候选池），因此需要引擎额外加载一份
全市场 × 短窗口的"广度面板"数据。聚合完成后投影回主面板、广度面板释放。

这两个算子的差别在于分组维度：
- `mean(mom20)` → 每个交易日一个市场标量，广播给当日所有股票
- `group_mean(mom20, industry)` → 每个 (交易日, 行业) 一个值，按个股行业 map 回去

---

## 逐行表达式的能力与限制

不含算子调用的纯表达式走 `pandas.eval`，支持：

- 算术：`+` `-` `*` `/` `**` `//` `%`
- 比较：`<` `<=` `>` `>=` `==` `!=`（比较结果为 0/1 浮点，可直接参与算术）
- 一元：`+` `-`
- 括号分组

不支持：
- 函数调用（`log()` 等一律不行，白名单算子见上文算子表）
- 属性访问（`df.col` 不行）
- 链式比较（`a < b < c` 不行）

如果需要更复杂的计算，拆成命名节点相互引用，或用算子表达式。

---

## where 掩码

`where` 是**求值后掩码**：先用 `expr` 算出原始值，再把 `where` 为 False 的
行置为 NaN。`where` 本身可以是纯逐行表达式或算子表达式（支持函数调用）。

```yaml
value:
  expr: "dv_ttm / pb"
  where: "pb > 0"     # PB 非正的行 value = NaN

# 也支持算子表达式（如引用其他因子或使用滚动窗口）
rel_mom:
  expr: "zscore(mom20)"
  where: "ma(close_hfq, 20) > 10"   # 只保留均线以上
```

`where` 不影响 ts 算子的滚动窗口——它只在最终输出阶段过滤。
如需在滚动之前过滤，用命名引用将条件置于前一节点。

---

## 伪列：引擎自动派生，因子表达式可直接引用

三个伪列不由数据层提供，由引擎在 preload 阶段按需附着：

| 伪列 | 来源 | 触发条件 |
|---|---|---|
| `idx_ret` | `config["benchmark"]` 指定的基准，经 backend `get_benchmark_bars` 取 hfq_close 日收益，按日期广播 | 表达式引用 `idx_ret` 时引擎自动派生。需要 backend 提供该方法且 benchmark 非空，缺一则 preload 直接报错 |
| `log_mktcap` | 由 `total_mv` 列取自然对数派生 | 表达式引用 `log_mktcap` 时引擎自动从 `total_mv` 派生。`total_mv` 必须在 extra_fields 中声明，否则 preload 报错 |
| `industry` | backend `get_stock_industries` 方法（鸭子类型） | 表达式引用 `industry` 或用到 `group_mean` / `group_rank` / `neutralize` 算子时触发。后端不提供该方法则 preload 直接报错 |

研究侧注意：`compute_factor` 对传入的 DataFrame 现算，**不会自动派生伪列**。
如果表达式引用了这些列，调用方必须自行在 df 上携带对应列。

---

## 策略侧消费

策略 YAML 的 `factor_specs` **只引用名字，不直写表达式**：

```yaml
factor_specs:
  - factor: mom20        # 引用 factors/library.yaml 里的名字
    weight: 1.0           # 可选，默认 1.0
    ascending: false      # 可选，默认 false（值越大得分越高）
```

loader 加载时解析引用闭包（所有传递依赖的因子节点）挂到策略实例的
`FACTOR_NODES` 属性上。引擎 preload 时据此做两件事：

1. **规划数据供给**：计算需要多少历史数据、哪些列、是否需要广度面板
2. **物化因子列**：一次性把所有因子值算出来，作为 DataFrame 的列

策略 `select()` 里直接用 `eval_factor_specs` 读列并合成得分：

```python
df = bars_to_df(filtered)
_, score = eval_factor_specs(df, self.FACTOR_SPECS)
```

`eval_factor_specs` 对各因子列做截面 percentile rank，按 weight
加权平均得到 score（值 ∈ [0, 1]，越大越优）。

如果因子列在 df 中不存在，会报错：
`ValueError: 因子列 'xxx' 不在截面数据里——引擎未物化（策略缺少 FACTOR_NODES？请经 strategy_loader 加载）`
——这通常意味着策略没有通过 loader 加载（手动 new 的策略实例缺少 `FACTOR_NODES`）。

自定义因子库路径（非默认 `factors/library.yaml`）：
```yaml
factor_library: my_lib.yaml   # 相对策略 YAML 所在目录解析
```

---

## 研究侧消费

研究脚本直接调用 `compute_factor` 对 DataFrame 现算：

```python
from btcore.factors.library import compute_factor
from research.factor_eval import calc_ic, calc_layered_returns

values = compute_factor("mom20", bars_df)       # 返回 Series
ic, rank_ic = calc_ic(values, forward_returns, date_col="trade_date")
layers = calc_layered_returns(values, forward_returns, date_col="trade_date")
```

`compute_factor` 按需递归计算引用链并 memo 化。传入的 df 需要是
MultiIndex `(trade_date, symbol)` 面板（纯逐行表达式也接受当日截面）。

批量计算：
```python
from btcore.factors.library import compute_factors

table = compute_factors(["mom20", "value", "mom_z"], bars_df)  # 返回 DataFrame
```

多因子合成（滚动 IC/ICIR 加权，`research/composite.py`）：
```python
from research.composite import combine_factors, evaluate_composite

score = combine_factors(table, forward_returns, method="icir", window=60)
ev = evaluate_composite(score, forward_returns)   # IC 汇总 + 分层累计收益
```

`t` 日权重只用 ≤ t-1 日的 IC 估计（rolling 后 shift(1)），无前视。

编程式 API：
```python
from btcore.factors.library import load_library, resolve_spec, resolve_closure

lib = load_library("my_factors.yaml")                       # 加载库
spec = resolve_spec({"factor": "mom20", "weight": 2.0}, lib)  # 解析 spec
nodes = resolve_closure(["mom_z"], lib)                      # 传递引用闭包
```

---

## 新增因子

1. 确认所需基础列由数据层提供（`extra_fields` 中声明）
2. 在 `factors/library.yaml` 里登记名字 + 表达式
3. 研究侧 `compute_factor` 验证 IC
4. 策略 YAML 的 `factor_specs` 里按名字引用

表达式校验在 `load_library` 加载阶段完成：不存在的列名、非法语法、
未知算子、引用环都会在加载时立即报错，不会拖到回测中途。

---

## 物化优化：公共子表达式消除（CSE）

`build_factor_plan` 在规划前对因子闭包做纯重写优化（`btcore/factors/cse.py`）：

- 完全重复的 `(expr, where)` 只求值一次，重复节点重写为对首个同构节点的引用；
- 出现 ≥2 次且不含坍缩算子的算子子树提取为合成节点（`__cse_N` 临时列），
  各表达式重写为引用该节点，随正常拓扑物化。

CSE 不改变任何物化结果（与无 CSE 逐值相等），只省重复计算；临时列在
物化后删除，策略与研究侧均不可见。

---

## 常见错误与报错

| 报错原文片段 | 原因 | 修正 |
|---|---|---|
| `因子名 'close' 与保留列名冲突` | 用了保留名 | 换一个名字 |
| `因子 'xxx' 表达式非法: unknown` | 表达式引用了不存在的列或在逐行表达式中调用了函数 | 检查列名拼写或用算子表达式替代 |
| `未知算子: xxx` | expr 中算子名不在白名单里 | 确认算子名拼写，对照算子表 |
| `因子引用存在环: A -> B -> A` | 因子形成了循环引用 | 打破循环，让 DAG 合法 |
| `窗口参数必须是正整数` | ts 算子的窗口参数不是正整数常量 | 确保窗口参数是 `20` 这样的数字字面量，不是表达式 |
| `winsorize 分位参数必须 ∈ (0, 0.5)` | winsorize 的 p 不在开区间内 | 改成 0.01~0.49 之间的值 |

---

## 研究侧的四个陷阱

### 1. warmup 不足

`compute_factor` 对传入的 DataFrame 现算，不自动向前延伸数据窗口。
若 df 起于 2024-01-02 而因子需 20 日滚动窗口（如 `ma(close_hfq, 20)`），
前 19 天均为 NaN。

**后果**：IC 计算、分层收益等分析因大量 NaN 产生偏差或报错。

**正确做法**：传入前在数据侧向前多取至少 `max(365, 最大窗口 × 1.5 + 10)` 天。
引擎 preload 阶段自动处理，研究侧需自行保证。

每个因子的精确 warmup 行数由 `infer_window` 静态推导（含嵌套表达式与
因子引用的传递累加；`ema` 这类无限记忆算子取 `3n-1` 工程近似），
`build_factor_plan` 返回的 `plan["windows"]` 逐节点透出，引擎 preload
时以 debug 日志输出，可用于核对"前 N 天因子为空"是否符合预期。

### 2. 口径自负

`compute_factor` 对传入的 DataFrame 计算。xsec 算子（rank / zscore 等）的
截面口径完全取决于传入的股票范围——传入中证 500 成分股，即得中证 500 内排名，
而非全市场排名。坍缩算子（mean / group_mean）同理。

**后果**：研究时用全市场验证的因子 IC，与回测时沪深 300 候选池的截面分布不同，
IC 不可直接外推。

**正确做法**：研究时使用与策略候选池一致的股票范围。

### 3. ascending 语义

`eval_factor_specs` 合成得分时，`ascending=true` 表示值越小得分越高
（如换手率），`ascending=false`（默认）表示值越大得分越高
（如动量）。

`compute_factor` 返回原始因子值，不做 rank 也不考虑 ascending。
研究侧自行 rank 时需确保方向一致。

**后果**：研究 IC 为正的因子，策略中 ascending 写反则排序方向颠倒。

**正确做法**：研究 IC 为正 → `ascending=false`；研究 IC 为负 → `ascending=true`。

### 4. 坍缩型因子不可用于截面评估

坍缩型因子（`mean(x)` / `group_mean(x, g)`，如 `pct_above_ma20`）同日所有股票取值
相同（截面无变异），因此以下研究工具对其**不适用**：

- **`calc_ic`**：常数因子与收益的相关系数未定义，每日 IC = NaN
- **`calc_layered_returns`**：无法对常数分档（`pd.qcut` 失败），输出为空
- **`combine_factors`**：截面 zscore 时标准差 = 0，合成全为 NaN

这不是函数写错了——宏观变量/市场广度指标天然没有截面区分度，不适合用 IC 或
分层回测评估。坍缩因子的正确评估方式应与基准收益比对（如计算时序 IC 或作为
择时信号评估）。

此外，`compute_factors` 简单路径（研究侧常用）不提供"广度面板"——坍缩算子
`mean()` 的聚合范围是传入 DataFrame 的股票池，而非全市场。若传入的是中证 500
成分股，得到的"市场广度"实际是中证 500 内部口径，与引擎路径的全市场口径不同。
