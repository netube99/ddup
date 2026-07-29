# CLI 与独立研究工具指南

ddup 提供两类用户可编程接口：**CLI 脚本**（`scripts/`，命令行直接调用）和**研究工具库**（`research/`，Python import 后使用）。CLI 侧重一键式工作流，研究库侧重可组合的纯函数 API——两者可混用，也可仅用 CLI 完成从因子评估到回测报告的全流程。

---

## 1. 快速开始

五分钟跑通从因子评估到回测报告的完整流水线：

```bash
# 0. 前置条件：已按 backend_guide 配置好 adapters/tushare.py（或自有后端）
#    已按 factor_library.md 在 factors/library.yaml 中定义因子

# 1. 因子评估：验证你的因子有区分力
python scripts/factor_eval.py mom20,vol_z --start 20240101 --end 20240630

# 2. 运行回测
python scripts/run.py strategies/examples/topk_momentum/config.yaml \
    --start 20240101 --end 20240630 --out result.db

# 3. 交叉验证：检查交易行为、风控触发、磨损是否合理
python scripts/cross_validate.py result.db

# 4. 生成 HTML 报告
python scripts/report.py result.db --out report.html

# 5. 参数扫描后多 run 对比（每 run 使用不同 --out 或不同策略）
python scripts/compare.py result.db --html compare.html
```

---

## 2. CLI 工具详解

### 2.1 `run.py` — 运行回测

```bash
python scripts/run.py <策略YAML> --start YYYYMMDD --end YYYYMMDD \
    [--capital N] [--out result.db] [--report report.html] [--no-report]
```

| 参数 | 说明 |
|------|------|
| `yaml` | 策略 YAML 配置文件路径（位置参数） |
| `--start` | 回测开始日期，YYYYMMDD 格式，**必填** |
| `--end` | 回测结束日期，YYYYMMDD 格式，**必填** |
| `--capital` | 初始资金（覆盖 YAML config 中的设置），float |
| `--out` | 回测结果库路径，默认 `:memory:`（内存库，不落盘） |
| `--report` | HTML 报告输出路径。缺省 `auto`（生成到 `<策略目录>/reports/<yaml名>_<起>_<止>.html`） |
| `--no-report` | 关闭报告生成 |

**数据库依赖**：行情数据从 `adapters/tushare.py` 的 `_DEFAULT_DB_PATH` 读取，不提供运行时切换数据源的参数——项目假定一次只对接一个数据库。

**输出**：终端打印核心统计指标（总收益、年化、夏普、最大回撤、成交笔数等），同时：
- `--out` 指定路径时落盘 SQLite 结果库（含 `runs`、`account_daily`、`trade_log` 表）
- `--report auto` 时生成 HTML 报告到策略目录的 `reports/` 子目录（该目录已在 `.gitignore` 中）

示例：

```bash
# 最简调用：内存运行 + 自动报告
python scripts/run.py strategies/examples/topk_momentum/config.yaml \
    --start 20240603 --end 20240628

# 落盘结果库，后续可离线生成报告/对比
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out results/my_run.db --no-report
```

### 2.2 `factor_eval.py` — 因子评估

```bash
python scripts/factor_eval.py <因子列表> \
    --start YYYYMMDD --end YYYYMMDD \
    [--universe CSI300] [--forward N] [--n-quantiles N]
```

| 参数 | 说明 |
|------|------|
| `factors` | 逗号分隔的因子名（来自 `factors/library.yaml`），位置参数，**必填** |
| `--start` | 开始日期，**必填** |
| `--end` | 结束日期，**必填** |
| `--universe` | 股票池，支持简称 `CSI300`/`CSI500`/`CSI1000` 或代码 `000300.SH` 等，默认全市场 |
| `--forward` | 前瞻收益天数（默认 5，即一周） |
| `--n-quantiles` | 分层回测分档数（默认 5） |

**输出**（三部分，终端打印）：

1. **IC 汇总表** — 每个因子的 Pearson IC 均值、ICIR、胜率、Rank IC
2. **分层回测** — 每档累计收益 + 多空收益（最高档-最低档）
3. **因子相关性矩阵** — 截面 Pearson 相关系数均值（≥2 个因子时）

示例：

```bash
# 评估单个因子
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630

# 多因子 + 沪深300成分池
python scripts/factor_eval.py mom20,vol_z,ep_z --start 20240101 --end 20240630 --universe CSI300

# 月度前瞻 + 10 档分层
python scripts/factor_eval.py roe_z,gross_margin_z --start 20240101 --end 20240630 \
    --forward 20 --n-quantiles 10
```

### 2.3 `report.py` — 单 run HTML 报告

```bash
python scripts/report.py <结果库.db> [--run-id N] --out report.html
```

| 参数 | 说明 |
|------|------|
| `db` | 回测结果库路径，位置参数，**必填** |
| `--run-id` | 指定 run_id，缺省取最新一次 |
| `--out` | HTML 输出路径，**必填** |

**输出**：单文件 HTML 报告，含净值曲线（内联 SVG）、回撤曲线、月度收益、基准对比、交易磨损、持仓复杂度、往返交易汇总、卖出来源归因、个股盈亏贡献 Top10、成交明细。

老 run（stats_json 为 NULL 或无此列）先经 schema 迁移补写 stats_json，若无则现场用 `stats.calculate_statistics` 重算后生成报告。

示例：

```bash
# 最新 run
python scripts/report.py result.db --out report.html

# 指定 run
python scripts/report.py result.db --run-id 3 --out run3_report.html
```

### 2.4 `compare.py` — 多 run 对比

```bash
python scripts/compare.py <结果库.db> [--runs 1,2,3] [--html compare.html]
```

| 参数 | 说明 |
|------|------|
| `db` | 回测结果库路径，位置参数，**必填** |
| `--runs` | 逗号分隔的 run_id 列表，缺省全部 |
| `--html` | 对比 HTML 报告输出路径（可选） |

**输出**：
- 终端打印关键指标对比表（总收益、年化、夏普、最大回撤、Calmar、胜率、成交笔数、换手率、交易成本、年化磨损等）
- `--html` 时生成对比报告：元信息表 + 指标对比表 + 归一化净值叠加曲线

注意：至少需要 2 个 run。

示例：

```bash
# 终端对比全部 run
python scripts/compare.py result.db

# 指定 runs + HTML 报告
python scripts/compare.py result.db --runs 1,2,3 --html compare.html
```

### 2.5 `cross_validate.py` — 交叉验证

```bash
python scripts/cross_validate.py <结果库.db> [--run-id N] [--strategy name] [--capital N]
```

| 参数 | 说明 |
|------|------|
| `db_path` | 结果库路径，位置参数，**必填** |
| `--run-id` | 指定 run_id |
| `--strategy` | 策略名称（仅用于输出标注） |
| `--capital` | 初始资金（用于动态阈值计算，0=自动从 config 读取） |

**验证维度**：

| 检查项 | 说明 |
|--------|------|
| 交易触发类型分布 | 检查是否有未预期的 trigger 类型 |
| 买卖比例平衡 | 卖出/买入比严重偏离 1 时告警 |
| 同日买卖冲突 | 同日同票既买又卖 |
| 风控强平 | RISK 触发笔数和日期 |
| 小资金交易磨损 | 按资金规模使用动态阈值检查成本占比合理性 |
| 单笔金额合理度 | 小单过多触发最低佣金的告警（≥10 万资金时） |
| 交易频率 | 日均 >10 笔告警 |
| 持仓上限 | 最大持仓数是否超 config.max_positions |
| 现金非负 | 是否存在负现金日 |
| 公司行为统计 | 分红/送转等 CORPORATE 触发笔数（INFO 输出） |
| 卖出分类统计 | 按 trigger 分组统计卖出的次数、平均金额、总盈亏 |

示例：

```bash
python scripts/cross_validate.py result.db --strategy topk_momentum
```

### 2.6 `bench_universe_preload.py` — 性能基准

对比全市场 preload 与指数成分并集 preload 的性能差异。适用于 tuning `get_universe` 策略。

```bash
python scripts/bench_universe_preload.py --start YYYYMMDD --end YYYYMMDD \
    [--yaml path] [--skip-load] [--skip-engine]
```

| 参数 | 说明 |
|------|------|
| `--start` | 开始日期，**必填** |
| `--end` | 结束日期，**必填** |
| `--yaml` | 策略 YAML 路径，默认 `strategies/examples/topk_momentum/config.yaml` |
| `--skip-load` | 跳过数据加载层基准 |
| `--skip-engine` | 跳过端到端基准 |

基准分两层：数据加载层（行数/耗时/内存）和端到端（`engine.run` 耗时）。指数并集取沪深300+中证500+中证1000 成分并集。

示例：

```bash
python scripts/bench_universe_preload.py --start 20240603 --end 20250630
```

### 2.7 `dump_fixtures.py` — Fixtures 生成

从真实数据库重新生成测试 fixtures（`tests/fixtures/*.parquet`）。数据库有更新或结构调整时使用。

```bash
python scripts/dump_fixtures.py
```

无参数，输出到 `tests/fixtures/`，生成 `bars.parquet`、`dividends.parquet`、`st.parquet`、`limits.parquet`、`components.parquet`、`benchmark_bars.parquet`、`trade_cal.parquet` 及辅助表 `moneyflow.parquet` / `cyq_perf.parquet` / `margin_detail.parquet`。

### 2.8 `check_anticorrupt.py` — 反破坏检查

提交前必须通过的架构约束检查，防止被移除的设计模式重新引入。

```bash
python scripts/check_anticorrupt.py
```

检查项目：

| 约束 | 说明 |
|------|------|
| 因子库无内置因子 | `btcore/factors/builtin.py` 不得存在 |
| engine 不 import builtin | engine.py 不得 import factors.builtin |
| Holding 无 last_adj_factor | types.py 的 Holding 不得有此字段 |
| Strategy ABC 无行为开关 | 不得有 take_profit_mode 等分支属性 |
| 因子层无旧 API | btcore/factors/__init__.py 不得暴露 Factor / FactorPipeline 等 |
| btcore 不依赖用户层 | btcore/ 不得 import strategies/ / factors/ / adapters/ |
| factors 层限制依赖 | factors/ 仅可依赖 btcore.factors |

---

## 3. 研究工具库 — 因子评估

`research.factor_eval` 提供纯函数式因子评估 API，所有指标计算是无状态的——输入 `pd.Series`，输出标量或 Series。

### 3.1 `calc_ic` / `summarize_ic`

```python
from research.factor_eval import calc_ic, summarize_ic

# factor_values: MultiIndex (trade_date, symbol) 的因子值 Series
# forward_returns: 同结构的未来收益 Series
ic, rank_ic = calc_ic(factor_values, forward_returns)
# ic: 每日 Pearson IC (Series, index=trade_date)
# rank_ic: 每日 Spearman Rank IC
# date_col: 日期列名，默认 "trade_date"

pearson_stats = summarize_ic(ic)
# {"ic_mean": ..., "ic_std": ..., "icir": ..., "ic_positive_ratio": ..., "n_days": ...}
spearman_stats = summarize_ic(rank_ic)
```

### 3.2 `calc_layered_returns`

```python
from research.factor_eval import calc_layered_returns

# n_quantiles: 分档数，默认 5
# 返回 {q: cumulative_return_series}，q=1 为最低档，q=N 为最高档
# date_col: 日期列名，默认 "trade_date"
layers = calc_layered_returns(factor_values, forward_returns, n_quantiles=5)

# 多空收益 = 最高档累计 - 最低档累计
# 注：当某些分位因数据不足被合并（duplicates="drop"）时，max/min 取实际存在的极端分位
long_short = layers[max(layers)].iloc[-1] - layers[min(layers)].iloc[-1]
```

### 3.3 `calc_factor_corr`

```python
from research.factor_eval import calc_factor_corr

# factor_df: MultiIndex (date, symbol) 宽表，每列一个因子
# 返回因子间平均截面 Pearson 相关矩阵
corr_matrix = calc_factor_corr(factor_df)
```

---

## 4. 研究工具库 — 报告生成

`research.report` 负责生成单文件 HTML 报告，纯 Python + 内联 SVG，零第三方依赖，离线可读。

### 4.1 `generate_report`

```python
from research.report import generate_report

# result: engine.run() 的返回 dict
#   {"account_daily": pd.DataFrame, "trade_log": pd.DataFrame, "statistics": dict, ...}
# out_path: HTML 输出路径
# title: 可选标题
generate_report(result, "report.html", title="我的策略报告")
```

注意：`result` 必须包含 `statistics`（含 `total_return`、`sharpe`、`max_drawdown` 等），引擎 `run()` 自动计算。

### 4.2 `generate_report_from_db`

```python
from research.report import generate_report_from_db

# 从结果库离线生成，无需重新运行回测
# run_id 缺省取最新 run
generate_report_from_db("result.db", "report.html", run_id=1)
```

老 run（stats_json 为 NULL 或无此列）先经 schema 迁移补写 stats_json，若无则现场用 `stats.calculate_statistics` 重算。

### 4.3 `generate_compare_report`

```python
from research.report import generate_compare_report

# run_ids 缺省取全部 run
generate_compare_report("result.db", "compare.html", run_ids=[1, 2, 3])
```

### 4.4 `load_runs` / `build_compare_table`

```python
from research.report import load_runs, build_compare_table

# 加载所有 run 的完整数据（meta + account_daily + trade_log + statistics）
runs = load_runs("result.db")
runs = load_runs("result.db", run_ids=[1, 2])

# 构建关键指标对比表（供 CLI 和自定义分析共用）
header, rows = build_compare_table(runs)
# header: ["指标", "run1 strategy", "run2 strategy", ...]
# rows: [["总收益率", "12.34%", "-5.67%", ...], ...]
```

---

## 5. 研究工具库 — 多因子合成

`research.composite` 对标 hikyuu ICMultiFactor 的滚动 IC 加权思路，将多个因子合成为单一截面得分。

### 5.1 `combine_factors`

```python
from research.composite import combine_factors

# factor_df: MultiIndex (trade_date, symbol) 宽表，每列一个因子值
# forward_returns: 同结构的未来收益（ic/icir 法用于权重估计，equal 法不需要）
# method: "equal" | "ic" | "icir"
# window: IC 估计滚动窗口（交易日），默认 60
# min_periods: 窗口最少有效观测数，默认 max(2, window//2)
# 返回 composite 得分 Series（同索引），前 ~window 日为 NaN（warmup）
composite = combine_factors(
    factor_df,
    forward_returns,
    method="icir",   # 推荐：IC/IC_std 加权的信息比率
    window=60,
)
```

**合成方法说明**：

| 方法 | 权重逻辑 | 适用场景 |
|------|---------|---------|
| `equal` | 等权加总截面 zscore | 快速 baseline |
| `ic` | 滚动 IC 均值加权 | IC 稳定的因子 |
| `icir` | 滚动 ICIR 加权（IC_mean / IC_std） | 推荐：同时考虑稳定性和方向 |

**前视保护**：t 日权重仅使用 ≤ t-1 日的 IC 估计（rolling 后 `shift(1)`），t 日 IC 依赖 t 日后才能实现的收益，不用于当日权重。

因子值在合成前自动做截面 zscore 标准化（去极值/中性化等深度预处理应在因子表达式内用 `winsorize`/`neutralize` 算子完成）。

### 5.2 `evaluate_composite`

```python
from research.composite import evaluate_composite

# 对合成因子做 IC + 分层回测评估
# n_quantiles: 分层数，默认 10
result = evaluate_composite(composite, forward_returns, n_quantiles=10)
# {"ic": {...}, "rank_ic": {...}, "layered": {q: cumulative_series, ...}}
```

---

## 6. 研究工具库 — Brinson 行业归因

`research.attribution` 提供 Brinson 归因分解，将策略相对于基准的超额收益拆分为**行业配置效应**、**选股效应**和**交互效应**。

### 6.1 `brinson_attribute`

```python
from research.attribution import brinson_attribute

result = brinson_attribute(
    db_path="result.db",          # 回测 DB（含 trade_log 表）
    provider_db="/path/to/market.db",  # 数据库（只读），含 index_member_all / sw_daily / index_weight
    start="20240601",
    end="20240701",
    index_code="000300.SH",       # 基准指数，默认 "000300.SH"
)

print(f"配置效应: {result['summary']['allocation_effect']:.4%}")
print(f"选股效应: {result['summary']['selection_effect']:.4%}")
print(f"交互效应: {result['summary']['interaction_effect']:.4%}")
print(f"超额收益: {result['summary']['total_excess_return']:.4%}")
```

### 6.2 数据依赖

| 数据 | 来源表 | 用途 |
|------|--------|------|
| 行业映射 | `index_member_all` | ts_code → 申万行业 (`l1_name`) |
| 行业收益 | `sw_daily` | L1 行业指数日收益率 (`pct_change`) |
| 基准权重 | `index_weight` | 基准指数成分股权重 |
| 策略持仓 | 回测 DB `trade_log` | 回放重建每日持仓，结合 `stk_factor_pro` 的 close 算市值 |

### 6.3 结果解读

返回值结构：

```python
{
    "summary": {
        "total_portfolio_return": ...,   # 策略累计收益
        "total_benchmark_return": ...,   # 基准累计收益
        "total_excess_return": ...,      # 超额收益
        "allocation_effect": ...,        # 行业配置贡献
        "selection_effect": ...,         # 选股贡献
        "interaction_effect": ...,       # 交互效应
        "unexplained": ...,              # 未解释残差
    },
    "industry_detail": {
        "银行": {
            "avg_portfolio_weight": ...,
            "avg_benchmark_weight": ...,
            "active_weight": ...,        # 主动权重 = portfolio - benchmark
            "portfolio_return": ...,     # 该行业策略累计收益
            "benchmark_return": ...,     # 该行业基准累计收益
            "allocation_effect": ...,
            "selection_effect": ...,
            "interaction_effect": ...,
            "total_contribution": ...,
        },
        ...
    },
    "daily": [{...}],                    # 逐日 Brinson 分解
    "exposure_summary": {                # 行业暴露概览
        "max_single_industry_weight": ...,
        "max_single_industry_name": ...,
        "effective_n_industries": ...,   # 有效行业数 = 1/Σ(w²)
        "top3_industries": [...],
    },
}
```

---

## 7. 典型工作流

### 7.1 因子研究 → 策略编写 → 回测 → 验证 → 报告

```bash
# 1. 因子初筛：快速看 IC/分层，初筛有区分力的因子
python scripts/factor_eval.py mom20,mom60,vol_z,rsi_14 \
    --start 20230101 --end 20240630 --universe CSI300

# 2. 因子相关性：排除高度共线因子（>0.7 考虑去重）
python scripts/factor_eval.py mom20,mom60,vol_z --start 20230101 --end 20240630

# 3. 编写策略 YAML + Python 类（按 strategy_guide.md）

# 4. 运行回测
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out results/v1.db

# 5. 交叉验证
python scripts/cross_validate.py results/v1.db --strategy my_strategy

# 6. 生成报告
python scripts/report.py results/v1.db --out results/v1_report.html
```

### 7.2 参数扫描与多 run 对比

```bash
# 多个参数组合共享同一个结果库（多次 run 默认追加新 run_id）
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out results/sweep.db --no-report
python scripts/run.py strategies/my_strategy/config_top10.yaml \
    --start 20240101 --end 20240630 --out results/sweep.db --no-report
python scripts/run.py strategies/my_strategy/config_top20.yaml \
    --start 20240101 --end 20240630 --out results/sweep.db --no-report

# 终端对比
python scripts/compare.py results/sweep.db

# HTML 对比报告（归一化净值叠加曲线一目了然）
python scripts/compare.py results/sweep.db --html results/sweep_compare.html
```

### 7.3 归因分析流程

如果想知道策略超额收益到底来自押对行业还是选对个股，在回测后运行 Brinson 归因：

```python
# research_script.py
from research.attribution import brinson_attribute

result = brinson_attribute(
    "results/v1.db",
    "/path/to/market.db",
    "20240101", "20240630",
)

# 看总览
s = result["summary"]
print(f"超额={s['total_excess_return']:.2%} "
      f"(配置={s['allocation_effect']:.2%} "
      f"选股={s['selection_effect']:.2%})")

# 看哪些行业贡献最大
for ind, d in sorted(
    result["industry_detail"].items(),
    key=lambda kv: abs(kv[1]["total_contribution"]), reverse=True
)[:5]:
    print(f"  {ind}: 总贡献={d['total_contribution']:.2%} "
          f"(配置={d['allocation_effect']:.2%} 选股={d['selection_effect']:.2%})")

# 行业暴露度
exp = result["exposure_summary"]
print(f"最大单行业: {exp['max_single_industry_name']} "
      f"({exp['max_single_industry_weight']:.1%}), "
      f"有效行业数: {exp['effective_n_industries']}")
```

### 7.4 多因子合成研究

```python
# composite_research.py
import pandas as pd
from research.factor_eval import calc_ic, summarize_ic
from research.composite import combine_factors, evaluate_composite

# 假设已有因子 DataFrame 和前瞻收益
# factor_df: MultiIndex (trade_date, symbol), columns=[mom20, vol_z, ep_z]
# fwd_ret: MultiIndex (trade_date, symbol) Series

# 对比三种合成方法
for method in ["equal", "ic", "icir"]:
    comp = combine_factors(factor_df, fwd_ret, method=method)
    ev = evaluate_composite(comp, fwd_ret)
    print(f"{method}: IC={ev['ic']['ic_mean']:.4f}  IR={ev['ic']['icir']:.2f}")

# 用最佳合成方法输出得分供策略使用
best = combine_factors(factor_df, fwd_ret, method="icir")
```

---

## 8. 参考速查表

### CLI 命令速查

| 命令 | 作用 |
|------|------|
| `python scripts/run.py <yaml> --start DATE --end DATE` | 运行回测 |
| `python scripts/factor_eval.py <factors> --start DATE --end DATE` | 因子 IC/分层/相关性 |
| `python scripts/report.py <db> --out report.html` | 单 run 报告 |
| `python scripts/compare.py <db> [--html compare.html]` | 多 run 对比 |
| `python scripts/cross_validate.py <db>` | 交叉验证 |
| `python scripts/bench_universe_preload.py --start DATE --end DATE` | 性能基准 |
| `python scripts/dump_fixtures.py` | 更新测试 fixtures |
| `python scripts/check_anticorrupt.py` | 架构约束检查 |

### 研究库 API 速查

| 模块 | 函数 | 输入 | 输出 |
|------|------|------|------|
| `research.factor_eval` | `calc_ic(fv, fwd)` | 因子值 Series, 前瞻收益 Series | (IC Series, RankIC Series) |
| | `summarize_ic(ic)` | IC Series | dict{ic_mean, icir, ...} |
| | `calc_layered_returns(fv, fwd)` | 因子值, 前瞻收益 | {分位: 累计收益 Series} |
| | `calc_factor_corr(df)` | 因子宽表 DataFrame | 相关矩阵 DataFrame |
| `research.report` | `generate_report(result, path)` | engine.run() 返回, 路径 | 写入 HTML |
| | `generate_report_from_db(db, path)` | DB 路径, 输出路径 | 写入 HTML |
| | `generate_compare_report(db, path)` | DB 路径, 输出路径 | 写入 HTML |
| | `load_runs(db)` | DB 路径 | [run dict, ...] |
| | `build_compare_table(runs)` | [run dict, ...] | (header, rows) |
| `research.composite` | `combine_factors(df, fwd, method)` | 因子宽表, 前瞻收益, 方法 | 合成得分 Series |
| | `evaluate_composite(comp, fwd)` | 合成得分, 前瞻收益 | {ic, rank_ic, layered} |
| `research.attribution` | `brinson_attribute(db, pdb, start, end, index_code, run_id)` | 回测DB, 库路径, 日期, 基准代码, run_id | 完整归因 dict |

### 常见参数速查

| 参数 | 出现工具 | 含义 |
|------|---------|------|
| `--start / --end` | run, factor_eval, bench | YYYYMMDD 日期范围 |
| `--out` | run, report | 输出路径（DB 或 HTML） |
| `--report` | run | HTML 报告路径或 `auto` |
| `--run-id` | report, cross_validate | 指定 run |
| `--universe` | factor_eval | CSI300/CSI500/CSI1000 |
| `--forward` | factor_eval | 前瞻天数，默认 5 |
| `--n-quantiles` | factor_eval | 分层数，默认 5 |
| `--capital` | run, cross_validate | 初始资金 |
| `window` | composite.combine_factors | IC 滚动窗口，默认 60 |
| `method` | composite.combine_factors | equal/ic/icir |
