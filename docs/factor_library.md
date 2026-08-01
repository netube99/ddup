# 因子库设计指南

`factors/library.yaml` 是唯一的因子定义入口。策略与研究两侧按名字消费同一份定义；数据供给规划、物化、公共子表达式共享由引擎自动完成，用户只需写 YAML。

---

## 1. 快速开始

仓库提供模板 `factors/library.yaml.template`（含 8 个示例因子）；`library.yaml` 本身不入库，复制模板后编辑：

```bash
cp factors/library.yaml.template factors/library.yaml
```

最简定义——在 `factors` 下加一条：

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"
    description: "20日动量（后复权）"
```

验证：

```bash
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630
```

策略侧在 `factor_specs` 里用 `factor: mom20` 按名引用，引擎自动物化（见 §10）。

---

## 2. 核心模型

- 所有因子都是 `(trade_date, symbol)` 双索引面板上的命名计算节点；每个节点一条表达式，节点间按名字引用形成 DAG，加载时拓扑排序并做环检测。
- 所有算子都是 grid→grid 变换（输入输出对齐同一面板网格），因此时序算子、截面算子、逐行算术可以在一条表达式里自由嵌套。
- 可选 `where` 子句是**后置掩码**：表达式先求值，`where` 为 False 的位置置 NaN，**不删行**——时序滚动窗口不会因缺行出现空洞。
- 两项引擎优化对用户透明，无需干预（行为见 §12、§13）：
  - 含坍缩算子的因子自动在**全市场**计算后投影回候选池面板；
  - 同名/重复的子表达式自动共享计算（CSE）。

---

## 3. 因子定义格式

`library.yaml` 顶层键为 `factors`，值是 `因子名 → 定义` 的映射：

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"     # 必填
    where: "pct_chg < 0.11"        # 可选，后置掩码
    description: "20日动量"         # 可选，建议写
```

| 键 | 必需 | 规则 |
|----|------|------|
| `expr` | 是 | 纯表达式或算子表达式（§5、§6），可引用其他因子名 |
| `where` | 否 | 同 `expr` 的表达式规则；为 False 处置 NaN（§8） |
| `description` | 否 | 任意文本 |

加载期校验（`load_library`）：逐条校验 `expr`/`where` 语法；因子名撞保留字、缺 `expr`、表达式非法、引用成环、**YAML 重复键**均直接报错（见 §15 反模式）。

### 3.1 保留字

以下 20 个名字禁止用作因子名（与契约列 / 派生列 / 伪列 / 算子名冲突）：

```
open, high, low, close, vol, amount,
adj_factor, pre_close, up_limit, down_limit,
open_hfq, high_hfq, low_hfq, close_hfq, pct_chg,
idx_ret, log_mktcap, industry, abs, log
```

### 3.2 命名惯例（建议）

- 小写蛇形：`mom20`、`vol_z`、`ep_ttm`
- 截面标准化后的因子加 `_z` 后缀：`mom_z`、`turnover_z`
- 直接引用数据列的因子沿用列名：`turnover_rate`、`pe_ttm`

---

## 4. 两条求值路径与语法差异

引擎按表达式是否含函数调用自动选择求值路径；两条路径可按名互相引用（§7），无需用户选择。

| 表达式特征 | 求值路径 | 能力 |
|-----------|----------|------|
| 无函数调用 | `pandas.eval`（numexpr 引擎） | 逐行算术 / 比较 / 布尔 |
| 含函数调用（如 `roc(x, 20)`） | 算子求值器（白名单） | 全部算子 + 算术 + 比较 + 布尔 |

**两条路径的语法差异**（易错点）：

| 语法 | 纯表达式路径 | 算子表达式路径 |
|------|:---:|:---:|
| `and` / `or` | 支持 | 支持 |
| `&` / `\|` | 支持 | **不支持**（报"不支持的运算符"） |
| 链式比较 `a < x < b` | 支持 | **不支持**（报"不支持链式比较"） |
| 一元 `+x` / `-x` | 支持 | 支持 |
| `not` | 支持 | **不支持** |

---

## 5. 纯表达式（pandas.eval 路径）

适用于不需要时序/截面变换的逐行计算。

### 5.1 支持的运算

| 类别 | 运算 |
|------|------|
| 算术 | `+` `-` `*` `/` `**` `%` `//` |
| 一元 | `+x` `-x` |
| 比较 | `<` `<=` `>` `>=` `==` `!=`（支持链式比较） |
| 布尔 | `and` `or` `&` `\|` `not` |

### 5.2 可引用的列

表达式中的裸标识符解析为面板列名，四类来源见 §9：契约列、引擎派生列、伪列、后端扩展字段列。

### 5.3 示例

```yaml
factors:
  low_turnover:
    expr: "turnover_rate"            # 直接引用扩展字段列
    description: "换手率"

  ep_ttm:
    expr: "1 / pe_ttm"
    where: "pe_ttm > 0"
    description: "盈利收益率（仅正 PE）"

  macd_golden:
    expr: "macd_dif > macd_dea"      # 比较运算产生 0/1
    description: "MACD 金叉信号"
```

### 5.4 限制

**禁止函数调用与属性访问**，加载期报错：

```yaml
expr: "np.log(close)"        # 错误：函数调用 → 改用 log(close)
expr: "df.close.mean()"      # 错误：属性访问
```

需要时序滚动或截面变换时改用算子表达式（§6）。

---

## 6. 算子表达式

算子共 21 个，分两类三族；白名单即全部可用函数，不存在运行时注册。

| 族 | 计算方向 | 形状 |
|----|----------|------|
| TS 时序（12 个） | 沿时间轴，按 symbol 分组 | 保形 |
| XSEC 保形（7 个） | 沿截面，按 trade_date 分组 | 保形 |
| XSEC 坍缩（2 个） | 沿截面聚合为标量，再广播/map 回网格 | 坍缩→投影 |

**参数规则**（加载期校验，违反即报错）：

- 算子参数必须按位置传，禁止关键字参数
- 窗口参数必须是**正整数字面量**（不能是表达式或变量），如 `ma(x, 20)`
- `winsorize` 的分位参数必须 ∈ `(0, 0.5)`，如 `0.01`、`0.05`
- 表达式常量必须是数字；裸标识符必须是列名或已定义因子名

### 6.1 TS 时序算子

按 symbol 分组、沿时间轴计算。所有 TS 算子是因果的（只用 ≤ 当日数据），无前视。

| 算子 | 签名 | 返回语义 |
|------|------|----------|
| `delay` | `(x, n)` | n 日前值 |
| `delta` | `(x, n)` | n 日变化量 `x - delay(x, n)` |
| `roc` | `(x, n)` | n 日收益率 `x / delay(x, n) - 1`；基准值为 0 处得 NaN |
| `ma` | `(x, n)` | n 日简单移动平均 |
| `ema` | `(x, n)` | n 日指数移动平均（span=n, adjust=False） |
| `std` | `(x, n)` | n 日滚动标准差 |
| `sum` | `(x, n)` | n 日滚动和（配比较表达式做事件计数） |
| `max` | `(x, n)` | n 日滚动最大值 |
| `min` | `(x, n)` | n 日滚动最小值 |
| `corr` | `(x, y, n)` | x 与 y 的 n 日滚动相关系数；分母为 0 处得 NaN |
| `beta` | `(x, y, n)` | x 对 y 的 n 日滚动回归斜率（含截距一元回归） |
| `resid_std` | `(x, y, n)` | x 对 y 的 n 日滚动回归残差标准差（特质波动率口径） |

窗口未满时结果为 NaN（rolling 的默认行为）。

```yaml
factors:
  bias20:
    expr: "close_hfq / ma(close_hfq, 20) - 1"
    description: "20日乖离率"

  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"       # TS 嵌套 TS
    description: "20日收益率波动率"

  sealed_days5:
    expr: "sum(fd_amount > 0, 5)"            # 事件计数
    description: "近5日封板天数"

  vol_corr:
    expr: "corr(close_hfq, amount, 20)"      # 两序列算子
    description: "价量20日滚动相关"
```

### 6.2 XSEC 保形算子

按 trade_date 分组做截面变换，形状不变。计算域为**因子所在面板**——引擎路径下是候选池口径（见 §12）。

| 算子 | 签名 | 返回语义 |
|------|------|----------|
| `abs` | `(x)` | 绝对值 |
| `log` | `(x)` | 自然对数 |
| `rank` | `(x)` | 截面百分位排名（0, 1] |
| `zscore` | `(x)` | 截面标准化 `(x - mean) / std`；当日截面 std 为 0 处得 NaN |
| `winsorize` | `(x, p)` | 截面缩尾：clip 到 `[分位(p), 分位(1-p)]`，p ∈ (0, 0.5) |
| `group_rank` | `(x, g)` | (date, g) 组内百分位排名（g 通常为 `industry`） |
| `neutralize` | `(x, g, size)` | 逐日对 g 哑变量 + size 做 OLS 取残差（行业/市值中性化）；当日有效样本 ≤ 回归变量数时全日 NaN |

```yaml
factors:
  mom_z:
    expr: "zscore(mom20)"
    description: "截面标准化动量"

  mom_winsor:
    expr: "zscore(winsorize(mom20, 0.05))"
    description: "5%缩尾 + 截面标准化"

  mom_industry_rank:
    expr: "group_rank(mom20, industry)"
    description: "行业内部动量排名"

  mom_neutral:
    expr: "neutralize(mom20, industry, log_mktcap)"
    description: "行业+市值中性化动量残差"
```

### 6.3 XSEC 坍缩算子

逐日把截面聚合为一个值，再回填到面板网格：

| 算子 | 签名 | 回填方式 |
|------|------|----------|
| `mean` | `(x)` | 全市场当日均值，按 trade_date 广播到所有个股（同日同值） |
| `group_mean` | `(x, g)` | (date, g) 组内均值，map 回每个个股所在组的值 |

```yaml
factors:
  pct_above_ma20:
    expr: "mean(close_hfq >= ma(close_hfq, 20))"
    description: "全市场站上MA20的股票占比（市场广度）"

  pct_sealed:
    expr: "mean(fd_amount > 0)"
    description: "全市场封板占比（市场情绪）"

  industry_mom:
    expr: "group_mean(mom20, industry)"
    description: "行业平均动量"
```

用户需要知道的坍缩算子行为（机制细节见 §12）：

- 引擎路径下，坍缩算子**始终在全市场计算**，与策略候选池大小无关；结果再投影回候选池面板。
- `group_mean` 的分组键请使用 `industry`：引擎按 `(trade_date, industry)` 把结果 map 回主面板。
- 坍缩因子同日所有（或同组）股票取值相同，**没有截面区分度**，不能做 IC/分层评估（见 §11.4 陷阱四）。

### 6.4 算子表达式中混用算术

算子只影响它包裹的子表达式，外层的逐行算术、比较、布尔照常执行：

```yaml
factors:
  bias20:
    expr: "close_hfq / ma(close_hfq, 20) - 1"        # 算子 + 算术

  sealed_days5:
    expr: "sum(fd_amount > 0, 5)"                    # 比较 → 0/1 → sum

  quality_composite:
    expr: "zscore(gpr) + zscore(npr) + zscore(rev_yoy)"  # 算子 + 算术 + 命名引用

  ema_bullish:
    expr: "(ema_5 > ema_20) + (ema_20 > ema_60) + (ema_60 > ema_250)"  # 布尔值可直接相加
    description: "EMA 多头排列得分（0-3）"
```

---

## 7. 命名引用与因子 DAG

因子可引用其他因子名，形成依赖 DAG；引擎按拓扑序求值，被引用方先计算。

### 7.1 混合引用

纯表达式与算子表达式可互相引用，不做区分：

```yaml
factors:
  mom20:
    expr: "roc(close_hfq, 20)"            # 算子表达式
  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"    # 算子表达式
  combo:
    expr: "mom20 / (vol_20d + 0.001)"      # 纯表达式引用两个算子因子
```

引用未定义的因子名：若 `factor_specs` 引用的入口因子名不存在，加载闭包时报 `未知因子 'xxx'`；若表达式内部引用了未定义的名字，加载不报错，物化求值时报 `未知列或因子引用 'xxx'`（算子路径）或 `因子 'xxx' 求值失败 … 缺少列: …`（纯表达式路径）。

### 7.2 窗口传递

因子 A 引用因子 B 时，A 所需的历史数据窗口自动累加 B 的窗口；多引用取最大值。窗口推导规则（`infer_window`，返回含基础列的总行数）：

| 算子 | 窗口开销（在子表达式窗口上累加） |
|------|-------------------------------|
| `delay` / `delta` / `roc` | n |
| `ma` / `std` / `sum` / `max` / `min` / `corr` / `beta` / `resid_std` | n - 1 |
| `ema` | 3n - 1（无限记忆的工程近似） |
| 全部 XSEC 算子、逐行算术/比较/布尔 | 0（取子表达式最大值） |

基础列窗口为 1。示例：

```
mom20   = roc(close_hfq, 20)          → 21 行（基础列 1 + 20）
mom_z   = zscore(mom20)               → 21 行（截面算子不消耗时间轴）
vol_20d = std(roc(close_hfq, 1), 20)  → 21 行（1 + 1 + 19）
combo   = mom20 / vol_20d             → max(21, 21) = 21 行
```

引擎 preload 按 `max(365, 最大窗口 × 1.5 + 10)` 个日历天自动向前延伸数据；研究侧需自行前伸（见 §11.4 陷阱一）。

### 7.3 环检测

加载 `library.yaml` 时对全库做环检测；A → B → A 成环直接报错 `因子引用存在环: ...`，不存在运行时死循环。

---

## 8. WHERE 子句

`where` 是对因子值的**后置掩码**：表达式先完整求值，`where` 为 False 的位置置 NaN。

```yaml
factors:
  ep_ttm:
    expr: "1 / pe_ttm"
    where: "pe_ttm > 0"              # 只保留正 PE

  seal_strength:
    expr: "fd_amount / (circ_mv * 10000)"
    where: "fd_amount > 0"           # 只保留封板日
```

要点：

- **置 NaN 而非删行**：若删行，后续 `ma(ep_ttm, 20)` 的滚动窗口会因中间缺行出现空洞。
- `where` 的表达式规则与 `expr` 完全一致：纯表达式或算子表达式均可，也可引用其他因子。
- `where` 也参与窗口推导（`where` 引用长窗口因子会抬高 warmup 需求）。
- `where` 不参与 CSE 重写（§13）。

---

## 9. 可用列

### 9.1 数据契约列

后端必须提供，缺列直接报错：

```
open, high, low, close, vol, pre_close, adj_factor, up_limit, down_limit
```

- `vol` 单位是手（1 手 = 100 股）
- `pre_close` 是交易所除权调整口径
- `adj_factor` 是后复权乘数（`close_hfq = close × adj_factor`）
- `amount` 不在必需契约内，但后端普遍提供，因子表达式可直接引用

### 9.2 引擎派生列

引擎从基础列精确派生，不向后端请求，表达式可直接使用：

| 派生列 | 公式 | 依赖 |
|--------|------|------|
| `open_hfq` | `open × adj_factor` | open, adj_factor |
| `high_hfq` | `high × adj_factor` | high, adj_factor |
| `low_hfq` | `low × adj_factor` | low, adj_factor |
| `close_hfq` | `close × adj_factor` | close, adj_factor |
| `pct_chg` | `(close - pre_close) / pre_close`（pre_close 为 0 处 NaN） | close, pre_close |

**建议**：价格类因子优先用 `*_hfq` 列（如 `roc(close_hfq, 20)` 而非 `roc(close, 20)`），自动获得后复权口径，不受除权跳变影响。

### 9.3 伪列（引擎按需附着）

表达式引用时才触发附着，因子作者无需手动管理：

| 伪列 | 含义 | 来源 / 前提 |
|------|------|-------------|
| `industry` | 行业分类（字符串） | `backend.get_stock_industries()`；引用 `industry` 或存在 `group_mean` 坍缩时附着 |
| `log_mktcap` | log 总市值 `log(total_mv)` | 由 `total_mv` 列派生（total_mv ≤ 0 处 NaN）；引擎路径自动补请求 `total_mv` |
| `idx_ret` | 基准指数日收益（按日广播） | `backend.get_benchmark_bars()`；需要引擎配置 benchmark（缺省从 `index_universe` 推导，否则回退 000300.SH；置空字符串则禁用），缺失时报错 |

研究侧注意：`compute_factors` **不自动附着伪列**——传入的 df 需自行携带 `industry` / `total_mv` / `idx_ret`（可用 `btcore.factors.plan.ensure_pseudo_columns` 准备）。

### 9.4 扩展字段

后端 `extra_fields` 中登记的任何列，因子表达式可直接使用（`pe_ttm`、`turnover_rate`、`dv_ttm`、`circ_mv`、`total_mv`、`fd_amount` 等），见 §17 与 `./backend_guide.md`。

---

## 10. 策略中使用因子

### 10.1 factor_specs

YAML 策略（`strategies/<name>/config.yaml`）：

```yaml
strategy: strategies.my_strategy:MyStrategy

factor_specs:
  - factor: mom_z          # 引用 library.yaml 中的因子名
    weight: 0.6            # 合成权重，默认 1.0
    ascending: false       # false=值越大排名越靠前（默认）
  - factor: vol_z
    weight: 0.4
    ascending: true        # true=值越小排名越靠前（低波异象）
  - factor: mkt_breadth20
    materialize_only: true # 仅物化，不参与得分合成
```

| 键 | 类型 | 必需 | 默认 | 含义 |
|----|------|:---:|------|------|
| `factor` | str | 是 | — | 因子库中的因子名 |
| `weight` | float | 否 | 1.0 | 得分合成权重 |
| `ascending` | bool | 否 | false | true=小值排名靠前 |
| `materialize_only` | bool | 否 | false | 仅物化列，不参与评分 |

`factor_specs` **只允许按名引用**——直写 `expr` 会在加载时报错，表达式必须先登记进 `library.yaml`。

### 10.2 materialize_only — 仅物化不评分

因子列仍物化到面板供 `calc_conditions()` / `select()` 读取，但跳过百分位排名与加权合成。适用场景：市场广度、情绪类坍缩因子需要可用但不应影响 top-k 选股得分。无需编造假权重来触发物化。

### 10.3 程序化构建

与 YAML 路径完全等价；条目用 `name` 键（与 `factor` 等价）：

```python
from btcore.strategy_loader import build_strategy

strategy = build_strategy(
    MyStrategy,
    config={"initial_capital": 100000, "max_positions": 10},
    factor_specs=[
        {"name": "mom_z", "weight": 0.6},
        {"name": "vol_z", "weight": 0.4, "ascending": True},
        {"name": "mkt_breadth20", "materialize_only": True},
    ],
)
```

### 10.4 在 select() 中消费因子得分

引擎 preload 阶段把 `factor_specs` 闭包内的全部因子物化为 bars 列；`select()` 里用 `eval_factor_specs` 合成得分：

```python
from btcore.strategy_tools import bars_to_df, eval_factor_specs

class MyStrategy(Strategy):
    def select(self, bars, account_snapshot, provider):
        df = bars_to_df(bars)
        factor_df, score = eval_factor_specs(df, self.FACTOR_SPECS)
        # score ∈ [0,1]，越大越优：每个评分因子先转截面百分位排名
        # （ascending=True 时小值得高分），再按 weight 加权平均；
        # materialize_only 条目只进 factor_df，不进 score
        top = score.nlargest(10).index.tolist()
        return {"buy": top, "sell": []}
```

### 10.5 Strategy 类变量

| 类变量 | 类型 | 含义 |
|--------|------|------|
| `FACTOR_SPECS` | `list[dict]` | 因子引用列表 `{name, weight, ascending, materialize_only}`；可在类级定义默认值，也可由 loader 注入实例级 |
| `FACTOR_NODES` | `dict \| None` | 因子传递引用闭包 `{name: {expr, where?}}`，由 strategy_loader 自动挂接；**不要手动设置**（手动实例化策略而绕过 loader 时引擎会报错提示） |
| `REQUIRED_FIELDS` | `list[str]` | 策略 `select()` 中命令式访问的额外列（因子列之外）；默认 `[open, high, low, close, vol, adj_factor]` |
| `CONDITION_FACTORS` | `set[str]` | 可选声明：`calc_conditions` / 条件单 handler 读取的因子名；loader 据此做交叉校验（与评分因子重叠或未登记时发警告） |

---

## 11. 因子评估（研究侧）

### 11.1 CLI 工具

```bash
# 基本用法：IC + 分层回测 + 相关性矩阵
python scripts/factor_eval.py mom20,vol_z,ep_z --start 20240101 --end 20240630

# 指定股票池（指数成分区间并集；支持简称 CSI300/CSI500/CSI1000 或指数代码）
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630 --universe CSI500

# 调整前瞻周期与分层数
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630 --forward 10 --n-quantiles 10

# IC 衰减模式：多前瞻期一张表（与 --forward 互斥）
python scripts/factor_eval.py cci_z,turnover_z --start 20240101 --end 20240630 --decay 1,3,5,10,20

# ML 模型分数与因子同口径评估（仅 panel scope；分数物化为 ml_<name> 列）
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630 --model path/to/model.onnx
```

| 参数 | 默认 | 说明 |
|------|------|------|
| `factors`（位置参数） | — | 逗号分隔的因子名（来自 library.yaml） |
| `--start` / `--end` | 必填 | YYYYMMDD |
| `--universe` | 全市场 | 指数代码或简称，取区间成分并集 |
| `--forward` | 5 | 前瞻收益天数 |
| `--decay` | 关 | 逗号分隔多前瞻期，输出 IC 衰减表；与 `--forward` 互斥 |
| `--n-quantiles` | 5 | 分层档数 |
| `--model` | 无 | ML 模型 ONNX 路径，与因子同口径评估 |

输出三部分：IC 汇总（Pearson IC / IR / 胜率 + RankIC / RankIR）、分层回测（各档累计收益 + 多空差）、因子相关性矩阵（≥2 个因子时）。

### 11.2 Python API

```python
from btcore.factors.library import load_library, compute_factors
from research.factor_eval import calc_ic, calc_layered_returns, summarize_ic, calc_factor_corr

library = load_library()
factor_df = compute_factors(["mom20", "vol_z", "ep_z"], bars_df, library)

# 前瞻收益（close_hfq 口径，与 CLI 一致）
fwd_ret = bars_df["close_hfq"].groupby("symbol").pct_change(5).shift(-5)

# IC
ic, rank_ic = calc_ic(factor_df["mom20"], fwd_ret)     # 每日截面 Pearson / Spearman
summarize_ic(ic)
# → {"ic_mean", "ic_std", "icir", "ic_positive_ratio", "n_days"}

# 分层回测：q=1 为因子值最低档，q=N 最高档
layers = calc_layered_returns(factor_df["mom20"], fwd_ret, n_quantiles=5)
# → {1: 累计收益 Series, ..., 5: ...}

# 因子相关性矩阵（按日截面 Pearson，再取日均）
corr_mat = calc_factor_corr(factor_df)
```

`compute_factor(name, df, library)` 为单因子版本，返回原始值 Series（不做 rank / 标准化）。`compute_factors` 要求 df 自带全部依赖列与伪列（§9.3）。

### 11.3 多因子合成

`research/composite.py` 提供滚动 IC/ICIR 加权合成；前视保护：t 日权重只用 ≤ t-1 日的 IC 估计。

```python
from research.composite import combine_factors, evaluate_composite

composite = combine_factors(
    factor_df, fwd_ret,
    method="icir",     # "equal" | "ic" | "icir"
    window=60,         # IC 估计滚动窗口（交易日）；前 ~window 日权重不可估计，得分为 NaN
    # min_periods=None → 自动取 max(2, window//2)
)
# 每因子先截面 zscore，再按带符号滚动 IC/IR 权重（归一化）逐日加权

result = evaluate_composite(composite, fwd_ret, n_quantiles=10)
# → {"ic": summarize_ic 汇总, "rank_ic": 同左, "layered": {分位: 累计收益 Series}}
```

### 11.4 研究侧常见陷阱

**陷阱一：warmup 不足**

- 症状：`compute_factor(s)` 返回的因子值前 N 天全是 NaN，IC 失真。
- 根因：`compute_factor` 对传入 df 现算，不自动向前延伸窗口；df 起于 2024-01-02 而因子需 20 日窗口，前 19 天均为 NaN。引擎 preload 会自动前伸（`max(365, 最大窗口 × 1.5 + 10)` 日历天），研究侧需自行保证。
- 修复：取数时向前多取 `max(365, 最大窗口 × 1.5 + 10)` 个日历天；精确窗口行数可用 `btcore.factors.ops.infer_window` 静态推导（规则见 §7.2）。`compute_breadth` 例外：它自动前伸窗口（与引擎同源的 `infer_windows` 推导），传入起止日期即可，无需手动扩展。

**陷阱二：口径自负**

- 症状：全市场评估 IC 很高，回测在沪深 300 候选池里 IC 远低于预期。
- 根因：XSEC 保形算子（`rank`/`zscore` 等）的截面口径完全取决于传入 df 的股票范围——传入中证 500 成分股即得中证 500 内排名。
- 修复：研究时使用与策略候选池一致的股票范围；不要用全市场 IC 外推窄池。

**陷阱三：ascending 语义写反**

- 症状：IC 为正的因子在策略里选出了排名最低的股票。
- 根因：`compute_factor` 返回原始值，不做 rank 也不考虑方向；`eval_factor_specs` 合成时 `ascending=false`（默认）= 值越大得分越高，`ascending=true` = 值越小得分越高。IC 为正 → `ascending=false`；IC 为负 → `ascending=true`。
- 修复：按 IC 符号校对 ascending。

**陷阱四：坍缩因子不可用于截面评估**

- 症状：对 `mean(x)` / `group_mean(x, g)` 因子调 `calc_ic` 全 NaN、`calc_layered_returns` 无法分档、`combine_factors` 合成全 NaN。
- 根因：坍缩因子同日（同组）所有股票取值相同，截面无变异——宏观/广度指标天然没有截面区分度。
- 修复：改用**时序维度**评估——与基准收益比对、作择时信号检验，或用 `compute_breadth` 流式输出日频标量序列（§16.5）。另注意：研究侧 `compute_factors` 简单路径没有引擎的全市场广度机制，`mean()` 的聚合范围就是传入 df 的股票池；传入中证 500 成分股时，"全市场站上 MA20 的比例"实际是"中证 500 内部口径"，与引擎路径的全市场口径不同。

---

## 12. 坍缩因子的供给与投影（用户可见行为）

含坍缩算子的因子与其他因子的计算域不同，引擎自动处理，用户无需配置：

- **保形算子**（zscore/rank/group_rank/neutralize 等）：在**候选池**面板上逐日计算，口径对齐策略选股域。
- **坍缩算子**（mean/group_mean）：在**全市场**面板上逐日聚合（"全市场站上 MA20 的比例"必须在全体股票上算才有统计意义），结果投影回候选池面板：
  - `mean` → 按 trade_date 广播（同日所有个股同值）
  - `group_mean(x, industry)` → 按 (trade_date, industry) map 回个股所在行业
- 因此**候选池大小不影响坍缩因子的值**——50 只候选池的策略拿到的仍是全市场口径。
- 被两侧同时引用的节点在两个面板各自计算：时序节点结果相同；截面保形节点按所在面板各自聚合（主面板=候选池口径，广度面板=全市场口径），互不干扰。
- 物化后引擎自动检查坍缩因子完整性：列缺失或存在 NaN 时输出告警日志；`validate_materialization(main_df, plan)` 返回 issues 列表（NaN 占比 > 5% 产生 warning 级条目）。
- 广度面板为瞬时加载（短窗口 + 窄列），物化投影后即释放，常驻内存只有主面板。

研究侧等价的流式入口是 `compute_breadth`（§16.5）：按 `chunk_days` 分片加载全市场数据，内存占用 O(chunk)。

---

## 13. CSE 公共子表达式共享（透明优化）

引擎在因子规划阶段自动做两类重写，**用户无需干预，物化结果与无优化逐值相等**：

1. **完全重复去重**：两个因子 `(expr, where)` 结构完全相同（如 `mom60` 与 `mom_60d` 同为 `roc(close_hfq, 60)`）时，后者重写为对前者的引用，只计算一次。
2. **公共子表达式提取**：出现 ≥ 2 次且不含坍缩算子的算子调用子树，提取为合成节点各求值一次；物化后合成节点的临时列自动删除。

限制：`where` 子句不参与重写；含坍缩算子的子树不提取。

---

## 14. 完整示例

> 示例中的 `pe_ttm`、`turnover_rate`、`dv_ttm`、`pb`、`buy_lg_amount`、`sell_lg_amount`、`circ_mv`、`fd_amount` 等列需后端在 `extra_fields` 中登记（§17）。

```yaml
factors:
  # 1. 直接引用数据列
  turnover:
    expr: "turnover_rate"
    description: "换手率"

  # 2. 单 TS 算子
  mom20:
    expr: "roc(close_hfq, 20)"
    description: "20日动量"

  # 3. 嵌套 TS
  vol_20d:
    expr: "std(roc(close_hfq, 1), 20)"
    description: "20日收益率波动率"

  # 4. 截面标准化
  mom_z:
    expr: "zscore(mom20)"
    description: "截面标准化动量"

  # 5. 带 where 过滤
  value:
    expr: "dv_ttm / pb"
    where: "pb > 0"
    description: "股息率/市净率（仅正 PB）"

  # 6. 命名引用链
  vol_z:
    expr: "zscore(vol_20d)"
  momentum_quality:
    expr: "mom_z - vol_z"
    description: "动量质量：高动量 + 低波动"

  # 7. 坍缩：市场广度（全市场聚合，同日广播）
  pct_above_ma20:
    expr: "mean(close_hfq >= ma(close_hfq, 20))"
    description: "全市场站上MA20的股票占比"

  # 8. 分组坍缩：行业均值（map 回个股）
  industry_mom:
    expr: "group_mean(mom20, industry)"
    description: "行业平均动量"

  # 9. 中性化
  mom_neutral:
    expr: "neutralize(mom20, industry, log_mktcap)"
    description: "行业+市值中性化动量残差"

  # 10. 综合混合：时序 + 截面 + 命名引用 + where
  mom_liq:
    expr: "zscore(mom20)"
    where: "ma(amount, 5) > 10000000"          # 5日均成交额 > 一千万才有效
    description: "流动性过滤的标准化动量"

  mf_divergence:
    expr: "(buy_lg_amount - sell_lg_amount) / amount - roc(close_hfq, 5)"
    where: "amount > 0"
    description: "大单流入但价未涨的背离"

  composite_score:
    expr: "zscore(mom20) * 0.5 + zscore(-vol_20d) * 0.3 + zscore(1 / pe_ttm) * 0.2"
    where: "pe_ttm > 0 and vol_20d > 0"        # 纯表达式 where：and/or、&/| 均可
    description: "动量50% + 低波30% + 价值20%"

  sentiment_mom:
    expr: "zscore(mom20) * (1 + pct_sealed * 10)"
    where: "pct_sealed > 0.01"
    description: "情绪调整动量（引用坍缩因子 pct_sealed）"

  # 11. 坍缩：封板占比（被 sentiment_mom 命名引用）
  pct_sealed:
    expr: "mean(fd_amount > 0)"
    description: "全市场封板股票占比"
```

---

## 15. 反模式与常见错误

### 15.1 表达式中写外来函数调用

```yaml
expr: "np.log(close)"        # 错误
expr: "math.sqrt(amount)"    # 错误
expr: "log(close)"           # 正确：21 个白名单算子即全部可用函数
```

白名单没有的能力：用算术/算子组合表达，或在数据层物化为扩展字段列。

### 15.2 因子名撞保留字

```yaml
factors:
  open: {expr: "..."}        # 与契约列冲突
  close_hfq: {expr: "..."}   # 与派生列冲突
  abs: {expr: "..."}         # 与算子名冲突
```

加载时报错 `因子名 'open' 与保留列名冲突`。保留字全表见 §3.1。

### 15.3 在 factor_specs 里直写 expr

```yaml
factor_specs:
  - expr: "roc(close_hfq, 20)"   # 加载时报错
```

`factor_specs` 只允许 `factor: name` 按名引用；表达式必须先登记进 `library.yaml`。

### 15.4 循环引用

```yaml
factors:
  a: {expr: "b + 1"}
  b: {expr: "a * 2"}
```

加载时报错 `因子引用存在环: ...`。

### 15.5 窗口/分位参数不合法

```yaml
expr: "ma(close_hfq, 0)"        # 窗口必须是正整数
expr: "ma(close_hfq, n)"        # 窗口必须是数字字面量，不能是变量
expr: "winsorize(mom20, 0.6)"   # 分位必须 ∈ (0, 0.5)
```

### 15.6 算子表达式里用 `&` / `|` 或链式比较

```yaml
expr: "sum((pct_chg > 0) & (amount > 0), 5)"    # 错误：报"不支持的运算符"
expr: "sum((pct_chg > 0) and (amount > 0), 5)"  # 正确
expr: "sum(0 < pct_chg < 0.1, 5)"               # 错误：不支持链式比较
```

纯表达式路径无此限制（§4 对照表）。

### 15.7 引用未定义的因子

```yaml
factors:
  composite:
    expr: "mom_z + ep_z"     # mom_z / ep_z 未定义：加载不报错，物化求值时报
                             # 「缺少列: ['ep_z', 'mom_z']」（算子路径报「未知列或因子引用」）
                             # 注：若 factor_specs 引用的因子名本身不存在，报「未知因子 'xxx'」
```

### 15.8 对坍缩因子做截面评估

见 §11.4 陷阱四：坍缩因子无截面区分度，IC/分层/合成都不可用；改用时间序列维度评估。

### 15.9 因子库 YAML 重复键

```yaml
mkt_breadth20:                     # 错误：与下方同名键重复
  expr: "pct_above_ma20"
mkt_breadth20:
  expr: "pct_above_close20"        # PyYAML 默认后者静默覆盖前者
```

`load_library` 对重复键直接报错并给出行号（如 `因子库重复键 'mkt_breadth20'（line 3）`）——库是手工维护的，静默覆盖是 typo 温床。

---

## 16. 参考表

### 16.1 算子速查表（21 个）

| 算子 | 签名 | 族 | 窗口开销 |
|------|------|-----|:---:|
| `delay` | `(x, n)` | TS | n |
| `delta` | `(x, n)` | TS | n |
| `roc` | `(x, n)` | TS | n |
| `ma` | `(x, n)` | TS | n-1 |
| `ema` | `(x, n)` | TS | 3n-1 |
| `std` | `(x, n)` | TS | n-1 |
| `sum` | `(x, n)` | TS | n-1 |
| `max` | `(x, n)` | TS | n-1 |
| `min` | `(x, n)` | TS | n-1 |
| `corr` | `(x, y, n)` | TS | n-1 |
| `beta` | `(x, y, n)` | TS | n-1 |
| `resid_std` | `(x, y, n)` | TS | n-1 |
| `abs` | `(x)` | XSEC 保形 | 0 |
| `log` | `(x)` | XSEC 保形 | 0 |
| `rank` | `(x)` | XSEC 保形 | 0 |
| `zscore` | `(x)` | XSEC 保形 | 0 |
| `winsorize` | `(x, p)` | XSEC 保形 | 0 |
| `group_rank` | `(x, g)` | XSEC 保形 | 0 |
| `neutralize` | `(x, g, size)` | XSEC 保形 | 0 |
| `mean` | `(x)` | XSEC 坍缩 | 0 |
| `group_mean` | `(x, g)` | XSEC 坍缩 | 0 |

窗口开销含义见 §7.2；`n` 为正整数字面量，`p` ∈ (0, 0.5)。

### 16.2 保留字

见 §3.1（20 个：9 契约列 + amount + 5 派生列 + 3 伪列 + abs/log）。

### 16.3 伪列

| 列名 | 含义 | 触发附着条件 | 前提 |
|------|------|-------------|------|
| `industry` | 行业分类 | 表达式引用 `industry`；或存在 `group_mean` 坍缩 | backend 提供 `get_stock_industries` |
| `log_mktcap` | log 总市值 | 表达式引用 `log_mktcap` | 面板含 `total_mv`（引擎自动补请求） |
| `idx_ret` | 基准指数日收益 | 表达式引用 `idx_ret` | 配置 benchmark 且 backend 提供 `get_benchmark_bars` |

### 16.4 派生列

| 列名 | 公式 |
|------|------|
| `open_hfq` | `open × adj_factor` |
| `high_hfq` | `high × adj_factor` |
| `low_hfq` | `low × adj_factor` |
| `close_hfq` | `close × adj_factor` |
| `pct_chg` | `(close - pre_close) / pre_close` |

### 16.5 Python API 速查

**因子库加载与计算**（`btcore.factors.library`）

```python
load_library(path=None) -> dict[str, dict]
  # 加载并校验 library.yaml（缺省 factors/library.yaml），返回 {name: {expr, where?, description?}}

compute_factor(name, df, library=None) -> pd.Series
  # 单因子原始值；df 为 (trade_date, symbol) 面板（纯逐行表达式可接受当日截面），
  # 依赖的伪列需 df 自带

compute_factors(names, df, library=None) -> pd.DataFrame
  # 批量计算，每列一个因子；同样要求 df 自带全部依赖列与伪列

compute_breadth(factor_name, backend, start, end, library=None, chunk_days=60) -> pd.Series
  # 流式计算坍缩因子为日频 Series（index=trade_date，值=当日全市场口径坍缩标量）。
  # 仅接受坍缩算子定义的因子，保形因子抛 ValueError；
  # 按 chunk_days 分片加载全市场数据，内存 O(chunk)；自动前伸 warmup 窗口
  # （同引擎 infer_windows 推导），评估区间头部即有值

resolve_spec(spec, library=None) -> dict
  # factor_specs 条目 → {name, weight, ascending, materialize_only}

resolve_closure(names, library=None) -> dict[str, dict]
  # 因子名的传递引用闭包 {name: {expr, where?, description?}}
```

**因子评估**（`research.factor_eval`）

```python
calc_ic(factor_values, forward_returns, date_col="trade_date") -> tuple[pd.Series, pd.Series]
  # → (每日 Pearson IC, 每日 Rank IC)

calc_layered_returns(factor_values, forward_returns, n_quantiles=5, date_col="trade_date") -> dict[int, pd.Series]
  # → {档号: 累计收益 Series}，q=1 最低档

summarize_ic(ic_series) -> dict
  # → {ic_mean, ic_std, icir, ic_positive_ratio, n_days}

calc_factor_corr(factor_df, date_col="trade_date") -> pd.DataFrame
  # → 因子截面相关性矩阵（按日 Pearson 的日均）

calc_ic_decay(factor_values, close_hfq, horizons, date_col="trade_date") -> pd.DataFrame
  # → 多前瞻期 IC 衰减表，index=horizon，
  #   列: ic_mean, ic_ir, ic_win, rank_ic_mean, rank_ic_ir, rank_ic_win, n_days
```

**多因子合成**（`research.composite`）

```python
combine_factors(factor_df, forward_returns, method="icir", window=60, min_periods=None) -> pd.Series
  # method: "equal" | "ic" | "icir"；min_periods 默认 max(2, window//2)

evaluate_composite(composite, forward_returns, n_quantiles=10) -> dict
  # → {ic, rank_ic, layered}
```

**策略工具**（`btcore.strategy_tools`）

```python
bars_to_df(bars) -> pd.DataFrame
  # 当日截面 dict-of-dicts → symbol 索引 DataFrame

eval_factor_specs(df, factor_specs) -> tuple[pd.DataFrame, pd.Series]
  # → (factor_df, score)；score ∈ [0,1] 加权百分位得分，factor_specs 为空时全 1.0
```

---

## 17. 与数据库后端的衔接

因子系统只需要后端提供扩展字段列，就能在表达式中引用：

1. 在后端 `extra_fields` 中登记列：

   ```python
   "extra_fields": {
       "pe_ttm": "daily_basic.pe_ttm",
       "dv_ttm": "daily_basic.dv_ttm",
       "turnover_rate": "daily_basic.turnover_rate",
   }
   ```

2. 在 `library.yaml` 中直接使用列名：

   ```yaml
   factors:
     ep_ttm:
       expr: "1 / pe_ttm"
       where: "pe_ttm > 0"
     div_yield:
       expr: "dv_ttm"
   ```

无需额外注册——列名对齐即可。伪列对应的后端方法（`get_stock_industries` / `get_benchmark_bars`）见 §9.3。详见 `./backend_guide.md`。
