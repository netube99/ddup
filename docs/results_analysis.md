# 回测结果、回放与归因

从具体 db + run_id 开始分析，先检查 runs.status。相同名称的策略不保证代码、配置、
因子、模型或数据版本相同。命令参数见 [CLI](cli_and_research.md)。

[结果表](#schema) · [事件与净额](#events) · [统计](#statistics) ·
[交叉验证](#cross-validation) · [回放](#debug) · [报告 API](#reports) · [Brinson](#attribution)

<a id="schema"></a>
## 结果库

DuckDB 的 runs/account_daily/trade_log/debug_snapshots/ml_predictions 按 run_id 累积。
run_id 与 trade_log.id 用 sequence，允许不连续；SQL 查询始终限制 run_id。
完整 DDL 的事实源为 btcore/database.py:SCHEMA_SQL。

| 表 | 列/主键与用途 |
| --- | --- |
| runs | run_id（PK）、created_at、strategy、start_date、end_date、initial_capital、config_json、status、stats_json |
| account_daily | (run_id,date)（PK）、cash、total_value、daily_pnl、cumulative_pnl、initial_capital、n_holdings |
| trade_log | id（PK）、run_id、date、symbol、side、trigger、price、shares、turnover、commission、stamp_tax、transfer_fee、slippage_amount、net_amount、reason |
| holdings | symbol（PK）、entry_date、entry_price、shares、cost、conditions_json、last_price、holding_days、updated_at；无 run_id |
| debug_snapshots | (run_id,date)（PK）、snapshot_json；仅 debug 模式 |
| ml_predictions | (run_id,date,symbol,model)（PK）、score；落盘范围由 ml_log 控制 |
| sweep_results | sweep 附加：id、label、params_json、stats_json；没有 run_id 外键 |

资金/价格 DOUBLE，日期与 JSON VARCHAR，ID/股数 BIGINT。
status 为 running/completed/failed。新 run 的 `config_json` 保留历史兼容的平铺配置键
（包括 CLI 覆盖后的 `initial_capital` / `max_positions`），并增加 `_run_manifest`：
版本化记录生效引擎设置、因子规格/过滤规则/因子闭包、策略类与策略文件哈希、引擎文件
哈希、Git commit、关键依赖版本、已加载模型契约和模型文件哈希，以及可识别的数据源身份。
分析脚本仍可从 JSON 顶层读取旧配置键；manifest 是附加字段，不改变 runs 表结构。

manifest 用排序 JSON 生成内容摘要。模型记录包含实际加载的 features、state features、
scaler、变换方式及 meta 内容；数据源若提供 `snapshot_id` 会记录该标识。没有快照标识时，
文件路径/大小/mtime（若后端暴露文件路径）只作为弱身份提示，manifest 标记 `identity_strength`
为 `weak`；不会散列整个行情库。自定义后端未暴露来源时，数据源为 `unknown`。

这些字段帮助区分运行声明和定位文件版本，但**不构成完整可重放快照**：Git commit 不包含
工作区状态，策略文件哈希只覆盖策略类所在文件，引擎哈希不覆盖全部动态模块，数据快照也
可能未知。旧 run 没有 `_run_manifest` 时按旧格式正常读取，但应视为追溯信息不完整；实盘
衍生结果遵循账本自身格式，不使用本回测 manifest。

清单序列化支持 JSON 基本值、字符串键 mapping、list/tuple、Path、日期/时间和可转为
Python 标量的 NumPy 值。非有限浮点（NaN/Inf）、非字符串 mapping 键、其它对象会在写入
run 行前 fail-fast；`_run_manifest` 是保留键，策略配置不得占用。旧实现把未知对象用
`str()` 写入，因此这项边界更严格；遇到自定义配置对象应先转成明确的 JSON 值。

holdings 每次初始化清空，只在 run 收尾写终态，不能用它查询某个历史 run 的逐日持仓。

读侧用 database.connect_result_db(read_only=True)，不要用 init_backtest_db 打开分析库：
后者会建表并清 holdings。SQLite 旧格式明确拒绝；只读报告不会自动转换旧库。

<a id="events"></a>
## 事件、金额与盈亏

| side | 含义 |
| --- | --- |
| BUY / SELL | 真实模拟成交；才计入交易笔数 |
| DIV | 税后现金分红，shares=0，net_amount 为到账额 |
| STK_DIV | 送转，shares 为送转后总股数，net_amount=0，trigger=CORPORATE |
| ADJUST | 实盘衍生库现金审计行，symbol 为空，net_amount 为现金调整 |

常见 trigger：MANUAL、TARGET、STOP_LOSS、TAKE_PROFIT、TRAILING_TP、ML_EXIT、
CORPORATE；sell_reasons 可覆盖手动卖出来源（如 TREND_BREAK），自定义 handler 使用其类型名。
reason 是说明字段，不等价于 trigger 的稳定分类键。

- price 已含滑点；回测 turnover 是滑点前名义成交额，不能直接用 price×shares 代替。
- 佣金、印花税、过户费与 slippage_amount 分列；重建现金时不要把滑点重复扣两遍。
- 实盘回放 price 是券商真实成交价，turnover=price×shares，没有额外模拟滑点。
- SELL 的 net_amount 是正的卖出净现金流，**不是利润**；不得用其正负计算胜率。
- 已实现回合盈亏来自 round_trip.trip_detail，按买入含费成本、卖出净额与分红配对。
  部分卖出/加仓采用 FIFO lot；同日公司行为应先作用于当时持仓。持仓股数重建时
  STK_DIV 是“设为送转后总数”，不能再把这个总数加到原持仓上。

```sql
SELECT trigger, COUNT(*) AS sell_count, SUM(net_amount) AS net_proceeds
FROM trade_log WHERE run_id = ? AND side = 'SELL' GROUP BY trigger;
```

该 SQL 只回答成交频次与净回款。盈亏和胜率应解析 stats_json.round_trip.trip_detail，
或从原始成交/费用/分红独立重建；至少抽查异常 trigger、单票集中、重复入场、持有期、
分月表现和成本结构中与本次问题有关的维度。

<a id="statistics"></a>
## 统计口径

Engine.run 返回 account_daily、trade_log、statistics、benchmark_nav、benchmark_code。
离线完整统计在 runs.stats_json；calculate_statistics 是纯函数，不会自己补取行情。

| 指标组 | 重点解释 |
| --- | --- |
| total_return / annualized_return | 总收益以 initial_capital 为分母；年化按交易日数，默认每年 244 日 |
| sharpe / sortino / max_drawdown / calmar | 首日收益计入波动/胜率，最大回撤峰值包含初始资金；max_drawdown 为非负幅度 |
| monthly_returns / yearly_returns | 期末值序列计算；首期口径需与完整区间收益区分 |
| round_trip | trip_detail、summary、open_positions；回合胜率与日胜率不同 |
| sell_source / symbol_contribution | 按卖出来源、个股归因，不用 SELL 净额冒充利润 |
| cost_breakdown / trading_friction | 佣金、印花税、过户费、滑点；年化成本拖累与资金占比 |
| management_complexity | 单日最多成交、交易日占比、单票市值等执行负担 |
| benchmark_compare | alpha/beta/information_ratio/tracking_error；依赖有效基准数据 |
| total_dividend_received | 已实现回合分红 |
| total_dividend_accrued | 已实现分红加期末未平仓 lot 分红 |
| max_dd_unrecovered | True 表示未修复；recovery_days 此时保留含谷值日的距期末计数，不是已恢复耗时 |

比较新旧版本时先固定输入与窗口，再看差异首次出现的日期、订单、价格、股数和费用。
必要时在独立目录运行旧版对照，不用 stash 改动共享工作区。修复后收益升降均可能，
旧实验的相对结论也需重新评估，不能仅把绝对收益换个数字。

<a id="cross-validation"></a>
## cross_validate 的范围

退出码是发现的问题数；检查实现见 research/cross_validate.py。
这些检查覆盖交易行为和经济性，不独立重算全部账务，也不能从成交日志证明未成交订单正确。

| 检查项 | 告警条件 |
|--------|---------|
| 交易触发类型分布 | 出现预期外 trigger。预期集合 = 引擎固定集（MANUAL/TARGET/CORPORATE）∪ 条件单注册表（含策略自定义 handler，如 DYNAMIC_STOP）；注册表之外的 trigger 降级为 INFO 提示（自定义 handler 只在策略进程注册，独立运行本脚本看不到） |
| 买卖比例平衡 | 卖出/买入比 > 3 或 < 0.3 |
| 同日买卖冲突 | 同日同票既有 BUY 又有 SELL |
| 交易磨损/资金比 | 超动态阈值（最低佣金开销×2 + 印花税底 + 按资金规模的可变上限）；资金 ≤5 万时降级为 INFO |
| 小单买入 | 资金 ≥10 万且 >50% 买入触发最低佣金——触发条件 = turnover × commission_rate < min_commission（引擎成本模型 commission=max(turnover×rate, min_commission)；rate=0 合法配置下全部买入触发），默认费率 ≈33333 元 |
| 交易频率 | 日均成交 > 10 笔 |
| 持仓上限 | 最大持仓数超 `config.max_positions` |
| 现金非负 | 存在负现金日 |
| 公司行为 / 卖出分类统计 | CORPORATE 笔数、按 trigger 分组的卖出次数/均额/总盈亏（INFO 输出） |

同时打印 stats 中的关键指标（键名与 stats 实际输出对齐：年化收益率/夏普/Calmar/平均持有天数等）与交易磨损明细；磨损阈值的最低佣金与印花税底从 run 的 config 读取（引擎费率可配置时不再失真）。

磨损阈值 = `2 × 买入次数 × min_commission / capital + stamp_tax_rate + 分档上限`。
分档上限：资金 ≤5 万为 3%，≤50 万为 1%，其余 0.5%；≤5 万超限仅 INFO。
资金取显式 --capital、run 配置，最后兜底 1,000,000；账本以 ledger_meta 初始资金为准。
小单条件是 turnover×commission_rate < min_commission（rate=0 时全部买入触发），
默认费率下约 33333 元。

逐条区分预期拒单、数据缺口、计算差异和策略磨损；高成本本身不等于引擎算错。
需要独立核验时按[核查清单](audit_checklist.md)选择受影响不变量，达到范围即停。

<a id="debug"></a>
## Debug 回放

run.py 没有 --debug 参数，需 Python 构造 Engine(debug=True, db_path=...)：

```python
from btcore.engine import Engine
engine = Engine(strategy, provider, debug=True, db_path="results/debug.duckdb")
result = engine.run("20240101", "20240630")
```

```bash
python scripts/replay.py results/debug.duckdb --run-id 1 --date 20240605 --symbol 000001.SZ
```

快照包含 account、pending、holdings_detail、bars_subset。pending 是当日收盘形成的
次日订单；不要将它当作当日已成交。bars_subset 源于主面板，可见 panel 模型列，
holding 决策点注入的分数不在原面板里，查 ml_predictions 而不是承诺 replay 必有。
replay 展示因子列有数量限制；无匹配快照或不存在 run 会报错。

<a id="reports"></a>
## 报告 Python API

```python
from research.report import (
    generate_report, generate_report_from_db, generate_compare_report,
    load_runs, build_compare_table,
)
generate_report(result, "report.html", title="策略报告")
generate_report_from_db("results/run.duckdb", "report.html", run_id=1)
generate_compare_report("results/run.duckdb", "compare.html", run_ids=[1, 2])
runs = load_runs("results/run.duckdb", run_ids=[1, 2])
header, rows = build_compare_table(runs)
```

生成独立 HTML，内联 SVG，无前端依赖。包含净值/回撤/月度收益、基准、磨损、执行负担、
往返回合、卖出来源、个股贡献与明细。load_runs 返回 meta/account_daily/trade_log/
statistics/benchmark_nav/benchmark_code；旧 run 缺 stats_json 时在内存重算，
缺少原始基准与该 run 终态持仓时不保证恢复完整指标。不会修改数据库。

<a id="attribution"></a>
## Brinson 行业归因

将策略相对基准的超额收益分解为**行业配置效应**、**选股效应**、**交互效应**。

### `brinson_attribute`（连接数据库）

```python
result = brinson_attribute(
    db_path="results/run.duckdb",       # 回测结果库（含 trade_log）
    provider_db="/path/to/market.duckdb",  # 行情数据库（只读打开）
    start="20240601",
    end="20240701",
    index_code="000300.SH",             # 基准指数，默认 000300.SH
    run_id=None,                        # 缺省取最新 run
)
```

### 数据依赖

| 数据 | 行情库表 | 用途 |
|------|---------|------|
| 行业映射 | `index_member_all` | ts_code → 申万 L1 行业（l1_name） |
| 行业收益 | `sw_daily` | L1 行业指数日收益率 |
| 基准权重 | `index_weight` | 基准成分股权重（聚合到行业并归一化） |
| 个股行情 | `stk_factor_pro` | close/pct_chg，用于持仓市值与个股收益 |
| 策略持仓 | 回测库 `trade_log` | 回放重建每日持仓 |

### 返回结构

```python
{
    "summary": {
        "total_portfolio_return": ...,   # 策略累计收益（逐日求和口径）
        "total_benchmark_return": ...,   # 基准累计收益
        "total_excess_return": ...,      # 超额收益
        "allocation_effect": ...,        # 行业配置贡献
        "selection_effect": ...,         # 选股贡献
        "interaction_effect": ...,       # 交互效应
        "unexplained": ...,              # 未解释残差
    },
    "industry_detail": {                 # 按行业分解，键为行业名
        "银行": {
            "avg_portfolio_weight": ..., "avg_benchmark_weight": ...,
            "active_weight": ...,        # 平均主动权重
            "portfolio_return": ...,     # 策略在该行业的累计收益贡献
            "benchmark_return": ...,
            "allocation_effect": ..., "selection_effect": ...,
            "interaction_effect": ...,
            "total_contribution": ...,   # 三效应之和
        }, ...
    },
    "daily": [                           # 逐日分解
        {"date", "portfolio_return", "benchmark_return", "excess_return",
         "allocation", "selection", "interaction"}, ...
    ],
    "exposure_summary": {
        "max_single_industry_weight": ..., "max_single_industry_name": ...,
        "effective_n_industries": ...,   # 有效行业数 = 1/Σ(w²)，按期间平均权重（未持有日计 0）
        "top3_industries": [{"name": ..., "weight": ...}, ...],
    },
}
```

**数据不足时不抛异常**，返回 `{"error": "原因", "summary": {}, "industry_detail": {}, "daily": [], "exposure_summary": {}}`（如 trade_log 无记录、bars 为空、sw_daily 无数据、benchmark_weights 无数据＝index 拼错/窗口无成分快照等），调用方需先检查 `"error"` 键。

**口径与限制**：

- 持仓沿交易日轴逐日结转——无成交日不丢（买入持有 N 天贡献 N 天归因）；`summary.total_portfolio_return` 是逐日策略收益之和，不能与回测复利总收益直接等同。日期轴取 provider 交易日历（`trade_cal` SSE 开市日，无表时回退 bars 行集并告警）：集中持仓整体停牌日也在轴上，当日组合收益按 0、基准收益照常计入；离线 parquet 入口无日历源，仍用 bars 行集
- 基准行业权重快照（约每月 2 次）**向前填充到日频**：快照延续至下次调整，首个快照前的日期因无已知基准构成而排除
- 基准收益按**全部基准行业**累加（含策略从未持有的行业）；`industry_detail` 同样含基准独有行业（持仓权重为 0、只承载配置效应），各行业 `total_contribution` 之和与 summary 三效应可对账；`portfolio_return` 为全部持仓的市值加权收益，无行业映射/缺 bar 的部分计入 `unexplained`
- `start/end` 与 run 实际区间不一致时告警：持仓自 run 起点重建（期初建仓不被丢失），归因统计按用户区间；离线数据必须具有明确的单指数与窗口标识
- 平均权重是期间平均（未持有日计 0，策略侧 Σ平均权重 = 1）；基准侧为已知快照日的均值

### `brinson_attribute_from_files`（离线 parquet）

```python
result = brinson_attribute_from_files(
    result_db="backtest_output/run.duckdb",
    industry_map="brinson_data/industry_map.parquet",
    sw_returns="brinson_data/sw_returns.parquet",
    benchmark_weights="brinson_data/benchmark_weights.parquet",
    bars="brinson_data/bars.parquet",
    run_id=1,                        # 默认 1（注意：不是"最新"）
    benchmark_code="000300.SH",      # 仅用于日志标注
)
```

| 参数 | 说明 |
|------|------|
| `result_db` | 回测结果库路径 |
| `industry_map` | parquet，须含 `ts_code, l1_name` 列（否则 ValueError） |
| `sw_returns` | parquet，index=trade_date，columns=行业名，值为小数 |
| `benchmark_weights` | parquet，index=trade_date，columns=行业名，值 0~1 |
| `bars` | 个股 bars parquet：MultiIndex `(trade_date, symbol)`，或含 `trade_date, symbol` 列（自动 set_index）；须含 `close, pct_chg` |

前三个 parquet 用 `scripts/dump_brinson_data.py` 导出（`--index` 指定基准指数）；提供 `--result-db` 与 `--start`/`--end` 时 dump 自动导出 `bars.parquet`，否则 bars 需自行导出。bars 起点必须 ≤ run 起始日，否则返回 error（期初持仓无法重建）；dump 对无效 `--index` 非零退出。任一文件不存在抛 `FileNotFoundError`。返回值遵循上方结构（含 `"error"` 键约定）。

---
配置效应反映行业权重选择，选股效应反映行业内表现，交互效应反映二者共同变化。
先检查 error 和 unexplained，再解释来源；静态行业映射、缺行情和收益聚合口径会限制结论。
