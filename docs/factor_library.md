# 因子库设计指南

`factors/library.yaml` 是唯一的因子定义入口。策略和研究两侧按名字消费同一份定义，
引擎自动处理数据供给规划、物化、CSE 优化——用户只需写 YAML。

---

## 1. 快速开始

```bash
# 1. 复制模板（如果还没有自己的 library.yaml）
cp factors/library.yaml.template factors/library.yaml

# 2. 编辑 factors/library.yaml，添加你的因子（见下方示例）

# 3. 用因子评估工具验证
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630
```

最简示例——在 `library.yaml` 的 `factors` 下加一条：

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"
    description: "20日动量（后复权）"
```

之后策略的 `factor_specs` 里用 `factor: mom20` 引用这个名字，引擎自动物化。

---

所有因子都是 `(trade_date, symbol)` 双索引面板上的计算节点。每个节点有一条表达式，可以是纯算术或算子调用，两者可混合嵌套。节点间按名字引用形成有向无环图（DAG），加载时自动拓扑排序并环检测。可选的 `where` 子句后置过滤（置 NaN 而不删行），保持时间轴窗口的完整性。

关键推论：

- 所有算子都是 grid→grid 变换——这是它们可以自由嵌套组合的根本原因
- `where` 不删行 → TS 算子的滚动窗口不会因为中间缺行而出现空洞
- 表达式被解析为节点依赖图 → CSE 公共子表达式消除是图变换，用户无需干预

---

## 2. 因子定义格式

`library.yaml` 的顶层键是 `factors`，下面是因子名到定义的映射：

```yaml
factors:
  因子名A:
    expr: "表达式"         # 必填
    where: "过滤条件"      # 可选
    description: "说明"    # 可选，但建议写

  因子名B:
    expr: "..."
```

### 2.1 两条求值路径

引擎根据表达式内容自动选择求值路径：

| 表达式特征 | 路径 | 能力 |
|-----------|------|------|
| 纯算术/比较，无函数调用 | `pandas.eval`（`btcore.factors.expr`） | `+ - * / ** % //` 比较 布尔 |
| 含算子调用（如 `roc(x, 20)`） | 算子求值器（`btcore.factors.ops`） | ts/xsec 算子 + 算术 + 比较 + 布尔 |

两条路径**可混用**——纯表达式可以引用含算子的因子，反之亦然。

### 2.2 保留字

以下名字不能用作因子名，因为它们与 bars 列或引擎派生列冲突：

```
open, high, low, close, vol, amount,
adj_factor, pre_close, up_limit, down_limit,
open_hfq, high_hfq, low_hfq, close_hfq, pct_chg,
idx_ret, log_mktcap, industry, abs, log
```

### 2.3 命名惯例建议

- 因子名用小写蛇形命名（`mom20`、`vol_z`、`ep_ttm`）
- 截面标准化后的因子加 `_z` 后缀（`mom_z`、`vol_z`）
- 直接使用原始数据列的因子用列名（`turnover_rate`、`pe_ttm`）

---

## 3. 纯表达式（pandas.eval 路径）

适用于不需要时序/截面变换的简单逐行计算。

### 3.1 支持的运算

| 类别 | 运算 |
|------|------|
| 算术 | `+` `-` `*` `/` `**` `%` `//` |
| 一元 | `+x` `-x` |
| 比较 | `<` `<=` `>` `>=` `==` `!=` |
| 布尔 | `&`（与） `\|`（或） |

### 3.2 引用基础列

表达式中可以直接写任何 bars DataFrame 里的列名。包括：

- **数据契约列**：`open`, `high`, `low`, `close`, `vol`, `pre_close`, `adj_factor`, `up_limit`, `down_limit`
- **引擎派生列**：`open_hfq`, `high_hfq`, `low_hfq`, `close_hfq`, `pct_chg`（见第 6 节）
- **扩展字段**：在 `adapters/` 后端 `extra_fields` 中登记的任何列（`pe_ttm`, `turnover_rate`, `dv_ttm` 等）
- **伪列**：`industry`, `log_mktcap`, `idx_ret`（引擎自动附着，见第 6 节）

### 3.3 示例

```yaml
factors:
  # 直接用扩展字段列
  low_turnover:
    expr: "turnover_rate"
    description: "换手率"

  # 多列算术
  ep_ttm:
    expr: "1 / pe_ttm"
    where: "pe_ttm > 0"
    description: "盈利收益率（仅正 PE）"

  # 比较运算（结果为 0/1）
  macd_golden:
    expr: "macd_dif > macd_dea"
    description: "MACD 金叉信号"
```

### 3.4 限制

**不能含函数调用或属性访问**。以下写法会报错：

```yaml
# 错误：函数调用
expr: "np.log(close)"

# 错误：属性访问
expr: "df.close.mean()"

# 正确：使用内置算子
expr: "log(close)"
```

如果 pandas.eval 处理不了的需求，用算子表达式替代（第 4 节）。

---

## 4. 算子表达式

因子表达式可以调用**内置算子**来实现时序滚动计算和截面变换。算子分两类三族：

| 族 | 方向 | 形状 |
|----|------|------|
| TS（时序） | 沿时间轴 groupby symbol | 保形 |
| XSEC 保形 | 沿截面 groupby date | 保形 |
| XSEC 坍缩 | 沿截面 groupby date | 坍缩后广播/map |

### 4.1 TS 时序算子

沿**时间轴**计算每个 symbol 自身的时间序列特征。

| 算子 | 参数 | 含义 |
|------|------|------|
| `delay(x, n)` | (Series, int) | n 日前值 |
| `delta(x, n)` | (Series, int) | n 日变化量 (x - delay(x,n)) |
| `roc(x, n)` | (Series, int) | n 日收益率 (x/delay(x,n) - 1) |
| `ma(x, n)` | (Series, int) | n 日简单移动平均 |
| `ema(x, n)` | (Series, int) | n 日指数移动平均 |
| `std(x, n)` | (Series, int) | n 日滚动标准差 |
| `sum(x, n)` | (Series, int) | n 日滚动和 |
| `max(x, n)` | (Series, int) | n 日滚动最大值 |
| `min(x, n)` | (Series, int) | n 日滚动最小值 |
| `corr(x, y, n)` | (Series, Series, int) | x 与 y 的 n 日滚动相关系数 |
| `beta(x, y, n)` | (Series, Series, int) | x 对 y 的 n 日滚动回归斜率 |
| `resid_std(x, y, n)` | (Series, Series, int) | x 对 y 的 n 日滚动回归残差标准差 |

**所有窗口参数必须是正整数常量**（不能是表达式）。

`corr` / `beta` / `resid_std` 使用闭式矩展开向量化实现（rolling 均值的可加性），不需要 Python 回调。

```yaml
factors:
  # 基础时序
  bias20:
    expr: "close_hfq / ma(close_hfq, 20) - 1"
    description: "20日乖离率"

  # 嵌套 TS（std 套 roc）
  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"
    description: "20日收益率波动率"

  # sum 用于事件计数
  sealed_days5:
    expr: "sum(fd_amount > 0, 5)"
    description: "近5日封板天数"

  # 两序列
  vol_corr:
    expr: "corr(close_hfq, amount, 20)"
    description: "价量20日滚动相关"
```

### 4.2 XSEC 保形算子

**逐日截面变换**，形状不变（每个 symbol 在该日期仍有一个值）。

| 算子 | 参数 | 含义 |
|------|------|------|
| `abs(x)` | (Series) | 绝对值 |
| `log(x)` | (Series) | 自然对数 |
| `rank(x)` | (Series) | 截面百分位排名（0~1） |
| `zscore(x)` | (Series) | 截面标准化（减均值除标准差） |
| `winsorize(x, p)` | (Series, float) | 截面缩尾，p∈(0,0.5) |
| `group_rank(x, g)` | (Series, Series) | 分组截面排名（如行业内排名） |
| `neutralize(x, g, size)` | (Series, Series, Series) | 行业+市值中性化取残差 |

参数要求：
- `winsorize` 的分位参数必须在 `(0, 0.5)` 内，如 `0.01`、`0.05`
- `group_rank` 的分组列通常为 `industry`
- `neutralize` 对行业哑变量 + size 做 OLS 回归取残差

```yaml
factors:
  mom_z:
    expr: "zscore(mom20)"
    description: "截面标准化动量"

  mom_winsor:
    expr: "zscore(winsorize(mom20, 0.05))"
    description: "5%缩尾 + 截面标准化动量"

  # 行业内排名（截面分组）
  mom_industry_rank:
    expr: "group_rank(mom20, industry)"
    description: "行业内部动量排名"

  # 行业+市值中性化
  mom_neutral:
    expr: "neutralize(mom20, industry, log_mktcap)"
    description: "行业市值中性化动量"
```

`neutralize` 需要 `industry` 和 `log_mktcap` 两个伪列；引擎检测到后自动附着（见第 6 节）。

### 4.3 XSEC 坍缩算子

**逐日截面聚合**为一个标量，再广播/map 回面板。与保形算子的关键区别：这些算子会**触发两路面板供给**——在**全市场**上聚合才有统计意义。

| 算子 | 参数 | 投影方式 |
|------|------|----------|
| `mean(x)` | (Series) | 按 date 广播到所有个股 |
| `group_mean(x, g)` | (Series, Series) | 按 (date, group) map 回个股 |

```yaml
factors:
  # 全市场站上 MA20 的股票比例（同日广播到所有个股）
  pct_above_ma20:
    expr: "mean(close_hfq >= ma(close_hfq, 20))"
    description: "市场广度——站上MA20的股票占比"

  # 全市场封板股票占比（市场情绪）
  pct_sealed:
    expr: "mean(fd_amount > 0)"
    description: "全市场封板占比（市场情绪广度）"

  # 行业平均动量（map 回每个个股所在行业的均值）
  industry_mom:
    expr: "group_mean(mom20, industry)"
    description: "行业平均动量"
```

**坍缩算子的运行机制**（见第 9 节）：
- `mean` / `group_mean` 在**全市场广度面板**上计算
- 结果**投影**回候选池主面板（market → 按 date 广播；group → 按 date+industry map）
- 用户不需要手动管理——引擎在 `build_factor_plan` 阶段自动规划

### 4.4 算子表达式中混用算术

算子表达式内可以自由混用逐行算术、比较和布尔运算——算子只影响它包裹的子表达式，外层运算照常。

```yaml
factors:
  # 算子 + 算术混用
  bias20:
    expr: "close_hfq / ma(close_hfq, 20) - 1"

  # 比较运算产生 0/1，给 sum 做事件计数
  sealed_days5:
    expr: "sum(fd_amount > 0, 5)"

  # 多因子加权（zscore + 算术 + 命名引用）
  quality_composite:
    expr: "zscore(gpr) + zscore(npr) + zscore(rev_yoy)"
```

---

## 5. 命名引用与因子 DAG

因子可以**引用其他因子**，形成计算依赖图（DAG）。引擎按拓扑序求值，加载期自动检测循环引用。

```yaml
factors:
  # 基础因子
  mom20:
    expr: "roc(close_hfq, 20)"

  # 引用 mom20
  mom_z:
    expr: "zscore(mom20)"

  # 引用 mom_z + 其他因子
  composite:
    expr: "mom_z + ep_z + vol_z * 0.5"
```

### 5.1 混合引用

纯表达式可以引用含算子的因子，不做区分：

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"          # 算子表达式

  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)" # 算子表达式

  combo:
    expr: "mom20 / (vol_20d + 0.001)"   # 纯表达式，引用了两个算子因子
```

### 5.2 窗口传递

当因子 A 引用因子 B 时，A 所需的历史数据窗口会**自动累加** B 的窗口。

> **两个窗口概念**：`window_cost`（算子本身额外消耗的天数，不含基础列）和 `infer_window`（包含基础列的完整所需行数，= 基础列 1 + 推导的窗口开销）。下例中的数值为 `infer_window` 返回值，因子列的实际 warmup 行数由 `build_factor_plan` 通过 `_to_calendar_days` 转换。

```
mom20 = roc(close_hfq, 20)        → 窗口 = 21（基础列 1 + TS n=20）
mom_z = zscore(mom20)              → 窗口 = 21（截面算子不消耗时间轴）
vol_20d = std(roc(close_hfq, 1), 20) → 窗口 = 21
combo = mom20 / vol_20d            → 窗口 = max(21, 21) = 21
```

TS 算子的窗口开销：`delay`/`delta`/`roc` 消耗 `n` 行（需要前 n 天基准值）；`ma`/`std`/`sum`/`max`/`min` 各消耗 `n-1` 行；`ema` 消耗 `3n-1` 行（工程近似）；`corr`/`beta`/`resid_std` 各消耗 `n-1` 行。

### 5.3 环检测

加载 `library.yaml` 时自动执行 DFS 三色标记环检测。如果 A → B → C → A 形成环，加载直接报错——不会出现运行时死循环。

---

## 6. WHERE 子句

`where` 是对因子值的**后置过滤**：表达式先求值，再把 `where` 为 `False` 的位置置为 `NaN`。

```yaml
factors:
  # 只保留正 PE 的 ep_ttm
  ep_ttm:
    expr: "1 / pe_ttm"
    where: "pe_ttm > 0"

  # 只保留上榜日的龙虎榜净买入比
  lhb_inflow:
    expr: "lhb_net_rate"
    where: "lhb_net_rate > 0"
```

### 6.1 为什么不删行？

`where` 置 `NaN` 而**不删除行**，是为了保持时间序列的窗口完整性。如果删除 `pe_ttm <= 0` 的行，后续 `ma(ep_ttm, 20)` 会因为中间缺行导致滚动窗口出现空洞。

### 6.2 where 的写法

`where` 可以使用纯表达式或算子表达式，与 `expr` 的规则一致：

```yaml
factors:
  # 纯表达式 where
  value:
    expr: "dv_ttm / pb"
    where: "pb > 0"

  # 算子表达式 where
  seal_strength:
    expr: "fd_amount / (circ_mv * 10000)"
    where: "fd_amount > 0"
```

`where` 不参与 CSE 重写。

---

## 7. 可用基础列速查

### 7.1 数据契约列

引擎从后端直接获取。如果你使用填表法（`GenericSQLBackend`），这些列来自你填的表单：

```
open, high, low, close, vol, pre_close, adj_factor, up_limit, down_limit
```

- `vol` 单位是手（1 手 = 100 股）
- `pre_close` 是交易所除权调整口径
- `adj_factor` 是后复权乘数（`close_hfq = close × adj_factor`）

### 7.2 引擎派生列

引擎在 preload 阶段自动从基础列计算，不向后端请求。可在表达式中直接使用：

| 派生列 | 公式 | 依赖基础列 |
|--------|------|------------|
| `open_hfq` | `open × adj_factor` | open, adj_factor |
| `high_hfq` | `high × adj_factor` | high, adj_factor |
| `low_hfq` | `low × adj_factor` | low, adj_factor |
| `close_hfq` | `close × adj_factor` | close, adj_factor |
| `pct_chg` | `close / pre_close - 1` | close, pre_close |

**建议**：因子表达式中优先使用 `*_hfq` 列（如 `roc(close_hfq, 20)` 而非 `roc(close, 20)`），自动获得后复权口径，不受除权跳变影响。

### 7.3 伪列（引擎附着）

引擎从后端获取附加数据后附着到面板。只有当因子表达式中引用了这些列时，引擎才会触发附着——不需要因子作者手动管理。

| 伪列 | 含义 | 来源 |
|------|------|------|
| `industry` | 行业分类（字符串） | `backend.get_stock_industries()` |
| `log_mktcap` | log 总市值 | 引擎从 `total_mv` 列计算 |
| `idx_ret` | 基准指数日收益率 | `backend.get_benchmark_bars()` |

### 7.4 扩展字段

你在 `adapters/` 后端的 `extra_fields` 里登记的任何列，都可以在因子表达式中直接使用：

```python
# adapters/tushare.py 的 FORM 中
"extra_fields": {
    "pe_ttm": "daily_basic.pe_ttm",
    "turnover_rate": "daily_basic.turnover_rate",
    "dv_ttm": "daily_basic.dv_ttm",
}
```

```yaml
# factors/library.yaml 中直接引用
factors:
  ep_ttm:
    expr: "1 / pe_ttm"
    where: "pe_ttm > 0"
```

---

## 8. 策略中使用因子

### 8.1 YAML 策略中的 factor_specs

```yaml
# strategies/my_strategy/config.yaml
strategy: strategies.my_strategy.MyStrategy

factor_specs:
  - factor: mom_z          # 引用 library.yaml 中的因子名
    weight: 0.6            # 合成权重，默认 1.0
    ascending: false       # false=值越大排名越靠前（默认）
  - factor: vol_z
    weight: 0.4
    ascending: true        # true=值越小排名越靠前（低波异象）
  - factor: ep_z
    weight: 0.3
    # ascending 缺省为 false

config:
  initial_capital: 100000
  max_positions: 10
```

每个 `factor_specs` 条目包含：

| 键 | 类型 | 必需 | 含义 |
|----|------|------|------|
| `factor` | str | 是 | 因子库中的因子名 |
| `weight` | float | 否 | 合成权重，默认 1.0 |
| `ascending` | bool | 否 | 是否升序排名（小值排前面），默认 false |
| `materialize_only` | bool | 否 | 仅物化不参与得分合成，默认 false |

#### 8.1.1 `materialize_only` — 仅物化不评分

`materialize_only: true` 告诉 `eval_factor_specs` 跳过该条目的评分合成——因子列仍物化到
`factor_df` 中供 `calc_conditions()` 判断信号时读取，但不参与百分比排名和加权平均。

适用场景：策略内部有复杂的因子组合逻辑（如在 `calc_conditions()` 中按市场广度决定
止损阈值），需要某个因子列可用，但不想让它在 `select()` 的 top-k 排名中产生影响。
声明为 `materialize_only` 即可——无需编造假权重来触发物化。

```yaml
factor_specs:
  - factor: mom_z
    weight: 0.6
  - factor: vol_z
    weight: 0.4
    ascending: true
  # mkt_breadth20 仅用于 calc_conditions 判断市场情绪，不参与选股得分
  - factor: mkt_breadth20
    materialize_only: true
```

程序化构建等价写法：

```python
factor_specs=[
    {"name": "mom_z", "weight": 0.6},
    {"name": "vol_z", "weight": 0.4, "ascending": True},
    {"name": "mkt_breadth20", "materialize_only": True},
]
```

### 8.2 程序化构建

```python
from btcore.strategy_loader import build_strategy

strategy = build_strategy(
    MyStrategy,
    config={"initial_capital": 100000, "max_positions": 10},
    factor_specs=[
        {"name": "mom_z", "weight": 0.6},
        {"name": "vol_z", "weight": 0.4, "ascending": True},
    ],
)
```

### 8.3 在 select() 中使用因子得分

引擎在 preload 阶段把因子值**物化**为 bars DataFrame 的列。策略的 `select()` 里通过 `eval_factor_specs` 读取并合成得分：

```python
from btcore.strategy_tools import bars_to_df, eval_factor_specs

class MyStrategy(Strategy):
    def select(self, bars, account_snapshot, provider):
        df = bars_to_df(bars)
        factor_df, score = eval_factor_specs(df, self.FACTOR_SPECS)

        # score 是 0~1 的合成得分，越大越优
        # 选前 10 只
        top = score.nlargest(10).index.tolist()
        return {"buy": top, "sell": []}
```

### 8.4 Strategy 类变量

| 类变量 | 类型 | 含义 |
|--------|------|------|
| `FACTOR_SPECS` | `list[dict]` | 因子引用列表，`[{name, weight, ascending, materialize_only}]` |
| `FACTOR_NODES` | `dict \| None` | 因子闭包，由 loader 挂接，用户不要手动设置 |
| `REQUIRED_FIELDS` | `list[str]` | 策略 `select()` 中命令式访问的额外列（因子列之外） |

---

## 9. 因子评估

### 9.1 CLI 工具

```bash
# 基本用法：评估 3 个因子的 IC 和分层回测
python scripts/factor_eval.py mom20,vol_z,ep_z \
    --start 20240101 --end 20240630

# 指定股票池（CSI500 成分股并集）
python scripts/factor_eval.py mom20,vol_z,ep_z \
    --start 20240101 --end 20240630 \
    --universe CSI500

# 调整前瞻收益周期和分层数
python scripts/factor_eval.py mom20 \
    --start 20240101 --end 20240630 \
    --forward 10 --n-quantiles 10
```

输出包括三部分：
1. **IC 汇总表**：每个因子的 Pearson IC、Rank IC、ICIR、胜率
2. **分层回测**：按因子值分 N 档，每档等权持有的累计收益曲线
3. **因子相关性矩阵**：各因子间截面 Pearson 相关的日均值

### 9.2 Python API

> 完整的函数签名速查见 §14.5。本节展示典型使用场景的串联方式。

```python
from research.factor_eval import calc_ic, calc_layered_returns, summarize_ic, calc_factor_corr
from btcore.factors.library import compute_factors, load_library

# 加载因子
library = load_library()
factor_df = compute_factors(["mom20", "vol_z", "ep_z"], bars_df, library)

# IC 分析
fwd_ret = bars_df["close_hfq"].groupby("symbol").pct_change(5).shift(-5)
ic, rank_ic = calc_ic(factor_df["mom20"], fwd_ret)
summary = summarize_ic(ic)
# → {"ic_mean": 0.034, "ic_std": 0.023, "icir": 0.54, "ic_positive_ratio": 0.67, "n_days": 118}

# 分层回测
layers = calc_layered_returns(factor_df["mom20"], fwd_ret, n_quantiles=5)
# → {1: Series, 2: Series, 3: Series, 4: Series, 5: Series}  累计收益曲线

# 相关性矩阵
corr_mat = calc_factor_corr(factor_df)
# → DataFrame, index/columns = 因子名
```

### 9.3 多因子合成

`research/composite.py` 提供滚动 IC/ICIR 加权合成：

```python
from research.composite import combine_factors, evaluate_composite

# 合成得分（前视保护：t 日权重只用 ≤ t-1 日的 IC 估计）
composite = combine_factors(
    factor_df, fwd_ret,
    method="icir",    # "equal" | "ic" | "icir"
    window=60,        # IC 估计的滚动窗口（交易日）
)

# 合成因子评估
result = evaluate_composite(composite, fwd_ret, n_quantiles=10)
# → {"ic": {...}, "rank_ic": {...}, "layered": {q: cumulative_returns}}
```

### 9.4 研究侧常见陷阱

以下四个陷阱在因子研究过程中反复出现，每个陷阱都遵循"症状 → 根因 → 修复"的诊断模式。

**陷阱一：warmup 不足**

- **症状**：`compute_factor` 返回的因子值前 N 天全是 NaN，IC 评估失真。
- **根因**：`compute_factor` 对传入的 DataFrame 现算，不自动向前延伸数据窗口。若 df 起于 2024-01-02 而因子需 20 日滚动窗口，前 19 天均为 NaN。引擎 preload 阶段自动处理前伸，但研究侧需自行保证。
- **修复**：传入前在数据侧向前多取 `max(365, 最大窗口 × 1.5 + 10)` 天。精确 warmup 行数可通过 `infer_window` 静态推导（含嵌套与传递累加；`ema` 取 `3n-1` 工程近似）。

**陷阱二：口径自负**

- **症状**：研究阶段用全市场数据评估的因子 IC 很高，回测时在沪深 300 候选池中 IC 远低于预期。
- **根因**：xsec 算子（`rank`、`zscore` 等）的截面口径完全取决于传入 DataFrame 的股票范围——传入中证 500 成分股，即得中证 500 内排名，而非全市场排名。坍缩算子（`mean`、`group_mean`）同理。
- **修复**：研究时使用与策略候选池一致的股票范围。不要用全市场 IC 直接外推到窄池。

**陷阱三：ascending 语义陷阱**

- **症状**：策略中因子排序方向与预期相反——IC 为正的因子选了排名最低的股票。
- **根因**：`compute_factor` 返回原始因子值，不做 rank 也不考虑 ascending。`eval_factor_specs` 合成得分时，`ascending=true` 表示值越小得分越高（如换手率），`ascending=false`（默认）表示值越大得分越高（如动量）。研究 IC 为正 → 对应 `ascending=false`；研究 IC 为负 → 对应 `ascending=true`。写反则排序方向颠倒。
- **修复**：按 IC 符号校对 ascending 设置。

**陷阱四：坍缩型因子不可用于截面评估**

- **症状**：对 `mean(x)` 或 `group_mean(x, g)` 类因子调用 `calc_ic` 返回全 NaN、`calc_layered_returns` 无法分档（`pd.qcut` 失败）、`combine_factors` 合成全 NaN。
- **根因**：坍缩因子同日所有股票取值相同，截面无变异。这不是函数写错了——宏观变量/市场广度指标天然没有截面区分度。
- **修复**：改用**时序维度**评估坍缩因子——与基准收益比对，或作为择时信号检验。**额外警示**：`compute_factors` 简单路径（研究侧常用）不提供引擎的广度面板机制——坍缩算子 `mean()` 的聚合范围是传入 df 的股票池，而非全市场。若传入中证 500 成分股，"全市场站上 MA20 的比例"实际是"中证 500 内部口径"，与引擎路径的全市场口径不同。

---

## 10. 进阶：两路面板供给机制

### 10.1 为什么需要两路面板？

因子中有两类截面对计算域有不同需求：

| 算子类型 | 计算域 | 原因 |
|----------|--------|------|
| 保形（zscore/rank 等） | **候选池** | 口径对齐策略选股域，候选人内部排名 |
| 坍缩（mean/group_mean） | **全市场** | 才有统计意义——"全市场站上 MA20 的比例"必须在全体股票上计算 |

引擎自动为含坍缩算子的因子**规划两路面板**：

- **主面板**：候选池 × 长窗口（max(365d, 最大 ts 窗口换算)）× 全列（基础列 + 伪列）
- **广度面板**：全市场 × 短窗口（最大 ts 窗口换算）× 窄列（仅因子依赖的列）

### 10.2 坍缩 → 投影

广度面板上计算坍缩节点后，值通过投影回到主面板：

- `mean(x)` → 按 `trade_date` 广播到主面板所有个股（同日同值）
- `group_mean(x, industry)` → 按 `(trade_date, industry)` map 到每个个股所在行业的值

主面板上的保形节点在候选池口径上计算。**用户无需手动管理**——`build_factor_plan` + `materialize` 是纯函数，引擎在 preload 阶段自动执行。

物化完成后引擎自动调用 `validate_materialization()` 检查坍缩因子的列存在性与 NaN 占比：
NaN 占比 >5% 时输出告警日志，返回 issues 列表供外部消费。

```python
from btcore.factors.plan import validate_materialization

issues = validate_materialization(bars_df, factor_plan)
# → [{"level": "warning", "message": "坍缩因子 'mkt_breadth20' NaN 占比 12.3% ..."}, ...]
```

### 10.3 数据量对比

对于一个典型的中证 500 选股策略（500 候选 vs 5000 全市场，120 天窗口）：

| 面板 | 股票数 | 窗口 | 列数 | 估算单元格数 |
|------|--------|------|------|------------|
| 主面板 | ~500 | ~190 天 | ~20 列 | ~1.9M |
| 广度面板 | ~5000 | ~30 天 | ~5 列 | ~0.75M |

广度面板是瞬时加载——短窗口 + 窄列，物化投影后由引擎释放。

---

## 11. 进阶：CSE 公共子表达式消除

引擎在 `build_factor_plan` 阶段对因子闭包做两类 CSE 重写，用户无需干预：

### 11.1 完全重复去重

如果两个因子的 `(expr, where)` 结构完全相同，后面的因子被重写为对第一个因子的引用：

```yaml
# 用户写了两个一样的因子
factors:
  mom20: {expr: "roc(close_hfq, 20)"}
  momentum_20d: {expr: "roc(close_hfq, 20)"}
```

引擎内部重写 `momentum_20d` 的 `expr` 为 `"mom20"`，只计算一次。

### 11.2 子表达式提取

出现 ≥2 次且不含坍缩算子的公共算子调用子树，被提取为合成节点 `__cse_0`、`__cse_1` 等。例如：

```yaml
factors:
  a: {expr: "zscore(roc(close_hfq, 20)) + zscore(roc(close_hfq, 5))"}
  b: {expr: "zscore(roc(close_hfq, 20)) - zscore(roc(close_hfq, 5))"}
```

引擎提取 `roc(close_hfq, 20)` 和 `roc(close_hfq, 5)` 为两个合成节点，各求值一次。物化后合成节点的临时列自动删除。

### 11.3 限制

- `where` 子句不参与 CSE 重写
- 含坍缩算子的子树不提取（两路面板语义不同）

---

## 12. 完整示例

### 从简单到复杂的因子设计

**示例 1：最简单的因子——直接引用数据列**

```yaml
factors:
  turnover:
    expr: "turnover_rate"
    description: "换手率"
```

**示例 2：单 TS 算子**

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"
    description: "20日动量"
```

**示例 3：嵌套 TS**

```yaml
factors:
  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"
    description: "20日收益率波动率"
```

**示例 4：截面标准化**

```yaml
factors:
  mom_z:
    expr: "zscore(mom20)"
    description: "截面标准化动量"
```

**示例 5：带 where 过滤**

```yaml
factors:
  value:
    expr: "dv_ttm / pb"
    where: "pb > 0"
    description: "股息率/市净率（仅正 PB）"
```

**示例 6：命名引用链**

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"
  mom_z:
    expr: "zscore(mom20)"
  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"
  vol_z:
    expr: "zscore(vol_20d)"
  momentum_quality:
    expr: "mom_z - vol_z"   # 动量质量：高动量 + 低波动
```

**示例 7：坍缩算子——市场广度**

```yaml
factors:
  pct_above_ma20:
    expr: "mean(close_hfq >= ma(close_hfq, 20))"
    description: "全市场站上MA20的股票占比（同日广播到所有个股）"
```

**示例 8：分组坍缩——行业均值**

```yaml
factors:
  industry_mom:
    expr: "group_mean(mom20, industry)"
    description: "行业平均动量（map 回个股）"
```

**示例 9：中性化**

```yaml
factors:
  mom_neutral:
    expr: "neutralize(mom20, industry, log_mktcap)"
    description: "行业+市值中性化动量残差"
```

**示例 10：超复杂混合因子**

> 示例中的 `pe_ttm`、`buy_lg_amount`、`sell_lg_amount`、`amount` 等列需先在 `adapters/` 后端的 `extra_fields` 中登记。见 `docs/backend_guide.md` §7。

```yaml
factors:
  # 基础因子
  mom20:
    expr: "roc(close_hfq, 20)"
  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"
  turnover:
    expr: "turnover_rate"

  # 时序 + 截面 + 命名引用 混用
  # 动量 zscore，仅流动性充裕时有效
  mom_liq:
    expr: "zscore(mom20)"
    where: "ma(amount, 5) > 10000000"        # 5日均量 > 一千万

  # 量价背离因子：大单流入但不涨
  mf_divergence:
    expr: "(buy_lg_amount - sell_lg_amount) / amount - roc(close_hfq, 5)"
    where: "amount > 0"
    description: "资金流向与价格背离"

  # 多维度合成（zscore + 命名引用 + 算术 + 比较 + 布尔）
  composite_score:
    expr: >
      zscore(mom20) * 0.5
      + zscore(-vol_20d) * 0.3
      + zscore(1 / pe_ttm) * 0.2
    where: "pe_ttm > 0 and vol_20d > 0"
    description: "动量 50% + 低波 30% + 价值 20%"

  # 含坍缩的情绪调整因子
  # 市场情绪好时（全市场封板率 > 3%），动量加权重
  sentiment_mom:
    expr: "zscore(mom20) * (1 + pct_sealed * 10)"
    where: "pct_sealed > 0.01"
    description: "情绪调整动量"
```

---

## 13. 反模式与常见错误

### 13.1 表达式中写函数调用

```yaml
# ❌ 错误
expr: "np.log(close)"
expr: "math.sqrt(amount)"

# ✅ 正确：用算子替代，或先物化为列再引用
```

算子白名单就是全部可用函数。如果白名单里没有，要么用算术/算子组合表达，要么在数据层物化为扩展字段列。

### 13.2 因子名与保留字冲突

```yaml
# ❌ 错误
factors:
  open: {expr: "..."}       # 与 bars 列冲突
  close_hfq: {expr: "..."}  # 与引擎派生列冲突
  abs: {expr: "..."}        # 与算子名冲突
```

加载时会报错 `因子名 'open' 与保留列名冲突`。

### 13.3 不走 library.yaml，直接写 expr

```yaml
# ❌ 错误：factor_specs 里不能直接写 expr
factor_specs:
  - expr: "roc(close_hfq, 20)"   # 加载时报错
    weight: 1.0
```

`factor_specs` 只允许引用因子库名字（`factor: name`）。表达式必须先登记在 `library.yaml` 中。

### 13.4 循环引用

```yaml
factors:
  a: {expr: "b + 1"}
  b: {expr: "a * 2"}
```

加载时报错 `因子引用存在环: b -> a -> b`。

### 13.5 窗口参数不合法

```yaml
# ❌ 错误
expr: "ma(close_hfq, 0)"       # 窗口必须是正整数
expr: "winsorize(mom20, 0.6)"  # 分位必须在 (0, 0.5)
```

### 13.6 坍缩算子在窄候选池上使用

坍缩算子 `mean` / `group_mean` 在**全市场**上计算。如果你的策略候选池只有 50 只股票，"全市场站上 MA20 的比例"就是在这 50 只里算——口径正确但统计意义不足。这是引擎机制保证的，不需要用户纠正，但应知晓。

### 13.7 命名引用的因子未定义

```yaml
factors:
  composite:
    expr: "mom_z + ep_z"
  # mom_z 和 ep_z 未在 library.yaml 中定义
```

加载时报错 `未知因子 'mom_z'`。

---

## 14. 参考表

### 14.1 算子速查表

| 算子 | 签名 | 族 | 窗口开销 |
|------|------|-----|----------|
| `delay` | `(x, n: int)` | TS | n |
| `delta` | `(x, n: int)` | TS | n |
| `roc` | `(x, n: int)` | TS | n |
| `ma` | `(x, n: int)` | TS | n-1 |
| `ema` | `(x, n: int)` | TS | 3n-1 |
| `std` | `(x, n: int)` | TS | n-1 |
| `sum` | `(x, n: int)` | TS | n-1 |
| `max` | `(x, n: int)` | TS | n-1 |
| `min` | `(x, n: int)` | TS | n-1 |
| `corr` | `(x, y, n: int)` | TS | n-1 |
| `beta` | `(x, y, n: int)` | TS | n-1 |
| `resid_std` | `(x, y, n: int)` | TS | n-1 |
| `abs` | `(x)` | XSEC 保形 | 0 |
| `log` | `(x)` | XSEC 保形 | 0 |
| `rank` | `(x)` | XSEC 保形 | 0 |
| `zscore` | `(x)` | XSEC 保形 | 0 |
| `winsorize` | `(x, p: float)` | XSEC 保形 | 0 |
| `group_rank` | `(x, g)` | XSEC 保形 | 0 |
| `neutralize` | `(x, g, size)` | XSEC 保形 | 0 |
| `mean` | `(x)` | XSEC 坍缩 | 0 |
| `group_mean` | `(x, g)` | XSEC 坍缩 | 0 |

### 14.2 保留字

```
open, high, low, close, vol, amount,
adj_factor, pre_close, up_limit, down_limit,
open_hfq, high_hfq, low_hfq, close_hfq, pct_chg,
idx_ret, log_mktcap, industry, abs, log
```

### 14.3 伪列

| 列名 | 含义 | 触发附着条件 |
|------|------|-------------|
| `industry` | 行业分类 | 表达式引用 `industry` 或使用 `group_rank`/`group_mean`/`neutralize` |
| `log_mktcap` | log 总市值 | 表达式引用 `log_mktcap` 或使用 `neutralize` |
| `idx_ret` | 基准日收益 | 表达式引用 `idx_ret` |

### 14.4 派生列

| 列名 | 公式 |
|------|------|
| `open_hfq` | `open × adj_factor` |
| `high_hfq` | `high × adj_factor` |
| `low_hfq` | `low × adj_factor` |
| `close_hfq` | `close × adj_factor` |
| `pct_chg` | `close / pre_close - 1` |

### 14.5 Python API 速查

**因子库加载**（`btcore.factors.library`）

```python
load_library(path=None) -> dict[str, dict]
  # 加载 library.yaml，返回 {name: {expr, where?, description?}}

compute_factor(name, df, library=None) -> pd.Series
  # 计算单个因子值

compute_factors(names, df, library=None) -> pd.DataFrame
  # 批量计算因子值，返回每列一个因子的 DataFrame

compute_breadth(factor_name, backend, start, end, library=None, chunk_days=60) -> pd.Series
  # 流式计算坍缩因子为日频 Series。仅接受坍缩算子（mean/group_mean 等）定义的因子，
  # 对保形因子抛出 ValueError。内部按 chunk_days 分片加载全市场数据，O(chunk) 内存。
  # 返回 index=trade_date 的 Series，每个值代表当日全市场口径的坍缩标量（如 0.35=35%）。

resolve_spec(spec, library=None) -> dict
  # 解析 factor_specs 条目

resolve_closure(names, library=None) -> dict[str, dict]
  # 解析命名引用闭包
```

**因子评估**（`research.factor_eval`）

```python
calc_ic(factor_values, forward_returns) -> tuple[pd.Series, pd.Series]
  # → (ic_series, rank_ic_series)

calc_layered_returns(factor_values, forward_returns, n_quantiles=5) -> dict[int, pd.Series]
  # → {quantile: cumulative_return_series}

summarize_ic(ic_series) -> dict
  # → {ic_mean, ic_std, icir, ic_positive_ratio, n_days}

calc_factor_corr(factor_df, date_col="trade_date") -> pd.DataFrame
  # → 因子相关性矩阵
```

**多因子合成**（`research.composite`）

```python
combine_factors(factor_df, forward_returns, method="icir", window=60, min_periods=None) -> pd.Series
  # → 合成得分 Series。min_periods 默认 None（自动 = max(2, window//2)）

evaluate_composite(composite, forward_returns, n_quantiles=10) -> dict
  # → {ic, rank_ic, layered}
```

**策略工具**（`btcore.strategy_tools`）

```python
bars_to_df(bars) -> pd.DataFrame
  # dict-of-dicts → symbol-indexed DataFrame

eval_factor_specs(df, factor_specs) -> tuple[pd.DataFrame, pd.Series]
  # → (factor_df, composite_score)

ConditionBuilder(rules)  # 条件单构建器，与因子系统无关
```

---

## 15. 与数据库后端的衔接

因子系统只需要后端提供扩展字段列，就能在表达式中引用。具体步骤：

1. 在 `adapters/` 后端的 `extra_fields` 中登记列：

   ```python
   "extra_fields": {
       "pe_ttm": "daily_basic.pe_ttm",
       "dv_ttm": "daily_basic.dv_ttm",
       "turnover_rate": "daily_basic.turnover_rate",
   }
   ```

2. 在 `library.yaml` 中直接使用这些列名：

   ```yaml
   factors:
     ep_ttm:
       expr: "1 / pe_ttm"
       where: "pe_ttm > 0"
     div_yield:
       expr: "dv_ttm"
   ```

无需任何额外的注册或配置——列名对齐即可。详见 `docs/backend_guide.md`。
