# CLI 与独立研究工具指南

ddup 提供两类用户接口：**CLI 脚本**（`scripts/`，命令行调用）和**研究工具库**（`research/`，Python import 后使用）。CLI 侧重一键式工作流；研究库为可组合的纯函数 API。两者可混用。

---

## 1. 快速开始

前置条件：已按 [backend_guide.md](./backend_guide.md) 配置行情数据库，已按 [factor_library.md](./factor_library.md) 在 `factors/library.yaml` 中定义因子。

```bash
# 1. 因子评估：验证因子有区分力
python scripts/factor_eval.py mom20,vol_z --start 20240101 --end 20240630

# 2. 运行回测并落盘结果库
python scripts/run.py strategies/examples/rolling_ranker/config.yaml \
    --start 20240101 --end 20240630 --out result.db

# 3. 交叉验证：检查交易行为与磨损是否合理
python scripts/cross_validate.py result.db

# 4. 生成 HTML 报告
python scripts/report.py result.db --out report.html

# 5. 多次回测写入同一结果库后做多 run 对比
python scripts/compare.py result.db --html compare.html
```

---

## 2. CLI 工具

### 2.1 `run.py` — 运行回测

```bash
python scripts/run.py <策略YAML> --start YYYYMMDD --end YYYYMMDD \
    [--capital N] [--out result.db] [--report report.html | auto] [--no-report]
```

| 参数 | 说明 |
|------|------|
| `yaml` | 策略 YAML 配置路径（位置参数） |
| `--start` / `--end` | 回测起止日期 YYYYMMDD，**必填** |
| `--capital` | 初始资金 float，覆盖 YAML config |
| `--out` | 结果库 SQLite 路径；缺省为内存库，不落盘 |
| `--report` | HTML 报告路径。缺省 `auto`：生成到 `<策略目录>/reports/<yaml名>_<起>_<止>.html`（该目录已在 `.gitignore`）；`--report` 裸写（不带值）等价于 `auto` |
| `--no-report` | 关闭报告生成 |

行情数据由 `adapters/tushare.py` 的 `_DEFAULT_DB_PATH` 决定，不提供运行时切换数据源的参数。

**输出**：终端打印 statistics 全部统计指标（嵌套分组递归展开）与成交笔数；`--out` 指定时写入结果库；非 `--no-report` 时生成 HTML 报告（内容见 2.3）。

**结果库结构**（多个 CLI 与研究库共用，同一库多次运行按 `run_id` 累积）：

| 表 | 内容 |
|----|------|
| `runs` | 每 run 一行：run_id、策略名、起止日期、初始资金、config_json、stats_json、status |
| `account_daily` | 逐日现金/总资产/盈亏/持仓数（按 run_id） |
| `trade_log` | 成交明细：date、symbol、side、trigger、price、shares、turnover、commission、stamp_tax、transfer_fee、slippage_amount、net_amount、reason |
| `debug_snapshots` | debug 模式逐日决策快照（供 `replay.py`） |
| `ml_predictions` | ML 模型逐日打分 |

trade_log 的 `side` 除 BUY/SELL 外还有两类公司行为行：`DIV`（现金分红，shares=0）
与 `STK_DIV`（送转增股，shares=送转后总股数，`trigger=CORPORATE`）。送转行是
stats 往返盈亏 / Brinson 持仓重建 / ML 回合配对重建持股事实的必需输入，勿删。

```bash
# 最简调用：内存运行 + 自动报告
python scripts/run.py strategies/examples/rolling_ranker/config.yaml \
    --start 20240603 --end 20240628

# 落盘结果库，后续离线生成报告/对比
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out results/my_run.db --no-report
```

### 2.2 `factor_eval.py` — 因子评估

```bash
python scripts/factor_eval.py <因子列表> --start YYYYMMDD --end YYYYMMDD \
    [--universe CSI300] [--forward N | --decay 1,3,5,10,20] [--n-quantiles N]
```

| 参数 | 说明 |
|------|------|
| `factors` | 逗号分隔因子名（来自 `factors/library.yaml`），位置参数；使用 `--model` 时可省略 |
| `--model` | ML 模型 ONNX 路径；模型分数物化为 `ml_<name>` 列后与因子同口径评估（仅支持 panel scope，详见 [ml_guide.md](./ml_guide.md)） |
| `--start` / `--end` | **必填** |
| `--universe` | 股票池：简称 `CSI300`/`CSI500`/`CSI1000`（对应 000300.SH/000905.SH/000852.SH）或其他指数代码（原样透传）；默认全市场。取成分快照并集为候选池，因子面板上再按 **point-in-time 成分**逐日过滤（与引擎逐日计算域、ml_train 训练域一致） |
| `--forward` | 前瞻收益天数，默认 5 |
| `--decay` | IC 衰减模式，逗号分隔天数（如 `1,3,5,10,20`）；不能与非默认的 `--forward` 同用 |
| `--n-quantiles` | 分层回测档数，默认 5 |

**口径与引擎同源**：

- 因子取值前伸 warmup 窗口（`fplan.main_days`，与引擎 preload 一致），滚动因子在评估窗口头部即有值，不再静默 NaN
- 坍缩因子（市场广度，如 `pct_above_ma20`）走全市场流式 `compute_breadth`，与引擎广度面板同口径；被其他因子表达式引用时会 fail-fast 提示（不支持嵌套坍缩）

**默认模式输出**（终端三段）：

1. **IC 汇总** — 每因子的 Pearson IC 均值、ICIR、胜率、RankIC、RankIR、有效天数
2. **分层回测** — 每档累计收益（Q1=因子值最低档）+ 多空收益（最高档-最低档）
3. **因子相关性矩阵** — 截面 Pearson 相关系数按日均值（≥2 个因子时）

**`--decay` 模式输出**：每因子一张 horizon × 指标衰减表（IC / IC IR / IC Win / RankIC / RankIR / Win / 天数）+ RankIC 首末 horizon 趋势总结（衰减/增强）。分层回测与相关性矩阵仍输出（分层固定用 `--forward` 默认 5d）。

```bash
# 单因子
python scripts/factor_eval.py mom20 --start 20240101 --end 20240630

# 多因子 + 沪深300成分池
python scripts/factor_eval.py mom20,vol_z,ep_z --start 20240101 --end 20240630 --universe CSI300

# 月度前瞻 + 10 档分层
python scripts/factor_eval.py mom_60d,turnover_z \
    --start 20240101 --end 20240630 --forward 20 --n-quantiles 10

# IC 衰减曲线
python scripts/factor_eval.py cci_z,turnover_z \
    --start 20240101 --end 20240630 --decay 1,3,5,10,20
```

### 2.3 `report.py` — 单 run HTML 报告

```bash
python scripts/report.py <结果库.db> [--run-id N] --out report.html
```

| 参数 | 说明 |
|------|------|
| `db` | 结果库路径，**必填** |
| `--run-id` | 指定 run_id，缺省取最新 run |
| `--out` | HTML 输出路径，**必填** |

输出单文件 HTML（内联 SVG，零依赖，离线可读），章节：核心指标、净值曲线（含基准叠加）、回撤曲线、月度收益、基准对比（Alpha/Beta/信息比率/跟踪误差）、交易磨损与成本拆解、持仓管理复杂度、往返交易汇总、卖出来源归因、个股盈亏贡献 Top10、成交明细。

老 run 无 stats_json 时自动迁移并现场重算统计指标（无基准数据时基准对比章节为空）。结果库中没有可用 run 时抛 `ValueError`。

### 2.4 `compare.py` — 多 run 对比

```bash
python scripts/compare.py <结果库.db> [--runs 1,2,3] [--html compare.html]
```

| 参数 | 说明 |
|------|------|
| `db` | 结果库路径，**必填** |
| `--runs` | 逗号分隔 run_id 列表，缺省全部 |
| `--html` | 对比 HTML 报告输出路径（可选） |

- 终端打印对比表，指标行：总收益率、年化收益率、夏普比率、最大回撤、Calmar 比率、日胜率、成交笔数、区间换手率、总交易成本、年化磨损拖累、单日最大成交笔数
- `--html` 生成对比报告：元信息表 + 指标对比表 + 归一化净值叠加曲线
- 不足 2 个 run 时报错退出（退出码 1）

### 2.5 `cross_validate.py` — 交叉验证

```bash
python scripts/cross_validate.py <结果库.db> [--run-id N] [--strategy name] [--capital N]
```

| 参数 | 说明 |
|------|------|
| `db_path` | 结果库路径，**必填** |
| `--run-id` | 指定 run_id，缺省取最新 run |
| `--strategy` | 策略名（仅用于输出标注） |
| `--capital` | 初始资金，用于磨损动态阈值；默认 0 = 从 run 的 config 读取，再兜底 40000 |

**退出码 = 发现的问题数**（0 = 通过），可直接用于脚本断言。

| 检查项 | 告警条件 |
|--------|---------|
| 交易触发类型分布 | 出现预期外 trigger。预期集合 = 引擎固定集（MANUAL/TARGET/CORPORATE）∪ 条件单注册表（含策略自定义 handler，如 DYNAMIC_STOP）；注册表之外的 trigger 降级为 INFO 提示（自定义 handler 只在策略进程注册，独立运行本脚本看不到） |
| 买卖比例平衡 | 卖出/买入比 > 3 或 < 0.3 |
| 同日买卖冲突 | 同日同票既有 BUY 又有 SELL |
| 交易磨损/资金比 | 超动态阈值（最低佣金开销×2 + 印花税底 + 按资金规模的可变上限）；资金 ≤5 万时降级为 INFO |
| 小单买入 | 资金 ≥10 万且 >50% 买入触发最低佣金 5 元 |
| 交易频率 | 日均成交 > 10 笔 |
| 持仓上限 | 最大持仓数超 `config.max_positions` |
| 现金非负 | 存在负现金日 |
| 公司行为 / 卖出分类统计 | CORPORATE 笔数、按 trigger 分组的卖出次数/均额/总盈亏（INFO 输出） |

同时打印 stats 中的关键指标（键名与 stats 实际输出对齐：年化收益率/夏普/Calmar/平均持有天数等）与交易磨损明细；磨损阈值的最低佣金与印花税底从 run 的 config 读取（引擎费率可配置时不再失真）。

### 2.6 `sweep.py` — 参数扫描批量回测

```bash
python scripts/sweep.py <sweep_config.yaml> --start YYYYMMDD --end YYYYMMDD \
    [--out sweep_result.db] [--capital N] [--dry-run]
```

| 参数 | 说明 |
|------|------|
| `sweep_config` | sweep 配置 YAML，**必填** |
| `--start` / `--end` | **必填** |
| `--out` | 汇总输出数据库，默认 `sweep_result.db` |
| `--capital` | 初始资金，透传给每次 run（覆盖 YAML config） |
| `--dry-run` | 仅打印参数组合，不运行 |

sweep 配置格式——`params` 键为 `.` 分隔的 YAML 嵌套路径，值为候选列表，取笛卡尔积：

```yaml
base: strategies/examples/rolling_ranker/config.yaml
params:
  config.top_k: [3, 5, 10]
  config.max_positions: [5, 10]
```

**执行方式**：每组参数在 base 配置上覆写后生成临时 YAML，子进程调用 `run.py` 执行（`--no-report` 关闭浪费的 HTML 生成）；失败组合打印 FAIL 并跳过，不中断扫描。

**输出**：每组参数作为**标准 run** 写入 `--out` 库的 `runs` 表（`config_json` 含参数，`compare.py`/`report.py` 原生可读）；同时写 `sweep_results` 表（`id, label, params_json, stats_json`，stats_json 内含 label 与 params 字段）保留参数标签汇总；终端按 收益/Sharpe/MDD 列打印汇总表。

参数展开逻辑可复用：

```python
from scripts.sweep import expand_params
combos = expand_params({"config.top_k": [3, 5], "config.max_positions": [5, 10]})
# → [("top_k=3, max_positions=5", {"config.top_k": 3, ...}), ...]
```

### 2.7 `replay.py` — 交易决策回放

从 debug 模式写入的 `debug_snapshots` 表回放每日决策上下文，用于定位某标的在某交易日的买卖依据。

```bash
python scripts/replay.py <result.db> [--run-id N] [--symbol SYM] [--date YYYYMMDD] [--list-symbols]
```

| 参数 | 说明 |
|------|------|
| `db` | 结果库路径，**必填** |
| `--run-id` | 指定 run_id，缺省取**最新 run**（与其他 CLI 一致） |
| `--symbol` | 过滤股票代码（该票当日无快照的日期跳过） |
| `--date` | 过滤日期 YYYYMMDD |
| `--list-symbols` | 按日期列出当日有快照的标的，不输出详细上下文 |

**详细模式输出**（按日期分组）：当日账户状态（现金/总资产/持仓数）、pending 买卖名单与买入条件单、每个持仓的股数/入场价/持仓天数/最新价/因子列值（最多 5 个）。

**前提**：回测时启用 debug 模式（`Engine(..., debug=True)`）且结果库落盘。无匹配快照时打印错误并以退出码 1 退出。

```bash
python scripts/replay.py result.db --date 20240605 --list-symbols
python scripts/replay.py result.db --symbol 000001.SZ
python scripts/replay.py result.db --symbol 000001.SZ --date 20240605
```

### 2.8 `dump_brinson_data.py` — Brinson 归因数据导出

从行情数据库一次性导出 Brinson 归因所需 parquet，供 `brinson_attribute_from_files()` 离线归因（见 6.4）。

```bash
python scripts/dump_brinson_data.py <行情库路径> [--out brinson_data] \
    [--index 000300.SH] [--start YYYYMMDD --end YYYYMMDD] [--result-db 回测库]
```

| 参数 | 说明 |
|------|------|
| `provider_db` | tushare 行情数据库路径，**必填** |
| `--out` | 输出目录，默认 `brinson_data` |
| `--index` | 基准指数代码，默认 `000300.SH`。**必须指定**：index_weight 表含多个指数，不过滤会把全部指数混成一个基准 |
| `--start` / `--end` | 日期范围（可选，缺省导出全表） |
| `--result-db` | 回测结果库；提供且同时给 `--start`/`--end` 时自动导出 `bars.parquet`（该库交易过的股票在区间内的 close/pct_chg） |

| 输出文件 | 内容 | 结构 |
|---------|------|------|
| `industry_map.parquet` | 股票→申万 L1 行业 | 列 `ts_code, l1_name` |
| `sw_returns.parquet` | 行业日收益 | index=trade_date，columns=行业名，值为小数（已除以 100） |
| `benchmark_weights.parquet` | 基准行业权重（单指数） | index=trade_date，columns=行业名，每日归一化到和为 1 |
| `bars.parquet` | 个股 bars（仅 `--result-db` 时导出） | MultiIndex (trade_date, symbol)，列 close/pct_chg |

### 2.9 `live.py` — 实盘账本与每日信号

实盘化的核心设计：**账本（ledger）是唯一持久化状态，与策略完全解耦**。
`ledger_fills`（append-only 真实成交）+ `ledger_meta`（账户元数据）是唯一手工
数据源；每次 `signal` 把账本灌进回测引擎全量回放（真实成交替代撮合、公司行为
走引擎原生路径、策略钩子逐日演化），末日 `_compute_pending` 的输出即次日操作单。
策略可随意切换——换一份 YAML 重新回放即得该策略口径的操作单。

```bash
python scripts/live.py init live/main.db --date 20260731 --cash 40000 [--positions p.yaml]
python scripts/live.py sync live/main.db sync.yaml          # 每日对账
python scripts/live.py signal live/main.db strategies/selected/xxx/config.yaml [--date D] [--out o.json]
python scripts/live.py status live/main.db
```

| 子命令 | 语义 |
|------|------|
| `init` | 建账：现金 + 可选已有持仓（`positions.yaml` 每条 `{symbol, shares, entry_date, entry_price}`，以 `OPENING` 条目入账；`entry_date/entry_price` 用于 holding_days 与 trailing 锚点重建，缺省空仓开局） |
| `sync` | 每日对账：追加今日成交 → 轻量回放（无因子，秒级）→ 衍生持仓与券商逐只比对，**不一致即回滚并报差异**；现金差额自动记 `ADJUST` 条目（超 100 元告警）。数据落后时也可用（估值用旧价不影响股数/现金对账） |
| `signal` | 全量回放 → 明日操作单（JSON）：`open_sells`（含 reason）/ `open_buys`（T 收盘预估股数，实际以明日开盘价定）/ `broker_conditions`（券商条件单：每只持仓的 TAKE_PROFIT/TRAILING_TP/STOP_LOSS 精确触发价，盘前设置当日有效）/ `notices`（除权预告、停牌、T+1 锁定）。同时重写衍生表 |
| `status` | 当前状态：最近一日 account_daily、持仓快照（`ledger_holdings`）、最近 10 条成交 |

`sync.yaml` 格式（全量账户信息一次性给到位）：

```yaml
date: 20260803
cash: 41233.55                                  # 券商可用资金
holdings: [{symbol: 600519.SH, shares: 100}]    # 券商实际持仓
fills:                                          # 今日实际成交（可为空）
  - {symbol: 000001.SZ, side: SELL, price: 12.34, shares: 1000,
     commission: 2.47, stamp_tax: 6.17, transfer_fee: 0.0, reason: TREND_BREAK}
```

- **账本即回测结果库**：`runs`/`trade_log`/`account_daily`/`holdings` 衍生表与回测
  同 schema（`report.py`/`cross_validate.py`/`replay.py` 直接消费）；公司行为
  （DIV/STK_DIV）回放时从分红表自动衍生落库，无需手工录入
- **成交是唯一真相源**：持仓/现金永远衍生，不可手改；对不上 = 漏录/错录成交
- **reason 字段**（= 回测 trigger）：冷却期记账（on_fills）与 ML holding 标签消费它；
  条件单触发离场如实记录（如 `TREND_BREAK`/`TRAILING_TP`），手动操作记 `MANUAL`
- **一致性保证**：回测 trade_log 灌入账本回放，衍生账户轨迹与回测逐日逐分钱一致、
  末日 pending_actions 逐键相等（`tests/test_live.py::TestBacktestParity` 锁定）
- 每日节奏：收盘后 ①更新行情库 ②`sync` ③`signal` ④次日盘前按操作单设券商条件单

### 2.10 开发/性能工具（一句话索引）

| 工具 | 用途 |
|------|------|
| `bench_universe_preload.py --start D --end D [--yaml path] [--skip-load] [--skip-engine]` | 性能基准：全市场 preload vs 沪深300+中证500+中证1000 成分并集 preload，分数据加载层（行数/耗时/内存）与端到端（engine.run 耗时）两层；调优 `get_universe` 时使用 |
| `dump_fixtures.py` | 从真实行情库重新生成 `tests/fixtures/*.parquet` 测试 fixtures（无参数；数据库结构或数据更新后使用） |
| `check_anticorrupt.py` | 提交前的架构约束静态检查（无参数，13 项结构检查；开发工具） |

---

## 3. 研究库 — 因子评估（`research.factor_eval`）

纯函数、无状态：输入 `(trade_date, symbol)` MultiIndex 面板 Series，输出标量/Series/DataFrame。日期索引名默认 `"trade_date"`，可用 `date_col` 参数改。

### 3.1 `calc_ic` / `summarize_ic`

```python
ic, rank_ic = calc_ic(factor_values, forward_returns, date_col="trade_date")
# ic:      每日截面 Pearson IC (Series, index=trade_date)；输入 dropna 后为空则返回空 Series
# rank_ic: 每日截面 Spearman Rank IC

stats = summarize_ic(ic)
# {"ic_mean", "ic_std", "icir", "ic_positive_ratio", "n_days"}
# 空输入返回全 0 dict
```

### 3.2 `calc_layered_returns`

```python
layers = calc_layered_returns(factor_values, forward_returns, n_quantiles=5)
# 返回 {q: 累计收益 Series}；q=1 为因子值最低档，q=N 为最高档
# 每档每日等权持有；数据不足的分位被合并时（duplicates="drop"）档位少于 N
# 多空收益 = layers[max(layers)].iloc[-1] - layers[min(layers)].iloc[-1]
```

### 3.3 `calc_factor_corr`

```python
corr = calc_factor_corr(factor_df)
# factor_df: MultiIndex 宽表，每列一个因子
# 返回因子间平均截面 Pearson 相关矩阵（按日求 corr 再取均值；样本 <3 的日期跳过）
# 因子 <2 列返回空 DataFrame；无有效日返回 NaN 矩阵
```

### 3.4 `calc_ic_decay`

```python
decay = calc_ic_decay(factor_values, close_hfq, [1, 3, 5, 10, 20])
# close_hfq: 同结构的后复权收盘价 Series（内部自行计算各 horizon 前瞻收益）
# 返回 DataFrame，index=horizon，列为：
```

| 返回列 | 含义 |
|--------|------|
| `ic_mean` / `ic_ir` / `ic_win` | 该前瞻期 Pearson IC 均值 / 信息比率 / IC>0 天数占比 |
| `rank_ic_mean` / `rank_ic_ir` / `rank_ic_win` | Spearman Rank IC 同口径 |
| `n_days` | 有效截面天数 |

用途：判断因子 alpha 随持有期拉长的衰减速度与 Rank IC 稳定性。

---

## 4. 研究库 — 报告生成（`research.report`）

单文件 HTML，内联 SVG，零第三方依赖。HTML 章节同 2.3。

```python
from research.report import (
    generate_report, generate_report_from_db, generate_compare_report,
    load_runs, build_compare_table,
)

# engine.run() 返回 dict → HTML（result 必须含 statistics 键，引擎 run() 自动计算）
generate_report(result, "report.html", title="我的策略报告")  # title 可选

# 从结果库离线生成；run_id 缺省取最新 run
generate_report_from_db("result.db", "report.html", run_id=1)

# 多 run 对比 HTML；run_ids 缺省取全部
generate_compare_report("result.db", "compare.html", run_ids=[1, 2, 3])

# 加载 run 列表；每项 {"meta", "account_daily", "trade_log", "statistics"}
# 老 run 无 stats_json 时现场重算（此时无基准对比与期末持仓浮盈数据）
runs = load_runs("result.db", run_ids=[1, 2])

# 构建指标对比表（compare.py 与对比 HTML 共用）
header, rows = build_compare_table(runs)
# header: ["指标", "run1 <策略名>", "run2 <策略名>", ...]
# rows:   [["总收益率", "12.34%", "-5.67%", ...], ...]（指标集同 2.4）
```

---

## 5. 研究库 — 多因子合成（`research.composite`）

将多个因子按滚动 IC/ICIR 加权合成为单一截面得分。

### 5.1 `combine_factors`

```python
composite = combine_factors(
    factor_df,         # MultiIndex (trade_date, symbol) 宽表，每列一个因子
    forward_returns,   # 同结构前瞻收益（仅 ic/icir 法用于权重估计）
    method="icir",     # "equal" | "ic" | "icir"
    window=60,         # IC 估计滚动窗口（交易日）
    min_periods=None,  # 窗口最少有效观测数，默认 max(2, window//2)
)
# 返回 composite 得分 Series（同索引，name="composite"）；ic/icir 法前 ~window 日为 NaN
#（warmup，等权 equal 法无窗口依赖）；method 非法时抛 ValueError
```

| 方法 | 权重逻辑 | 适用场景 |
|------|---------|---------|
| `equal` | 等权平均截面 zscore | 快速 baseline |
| `ic` | 滚动 IC 均值加权 | IC 稳定的因子 |
| `icir` | 滚动 ICIR（IC_mean/IC_std）加权 | 推荐：兼顾方向与稳定性 |

行为契约：

- 合成前每因子自动做截面 zscore 标准化（去极值/中性化等深度预处理应在因子表达式内用 `winsorize`/`neutralize` 算子完成）
- **前视保护**：t 日权重只用 ≤ t-1 日的 IC 估计（rolling 后 shift(1)）
- 权重按绝对值归一化；某因子当日无 IC 时权重补 0，全部因子无 IC 的日子保持 NaN

### 5.2 `evaluate_composite`

```python
result = evaluate_composite(composite, forward_returns, n_quantiles=10)
# {"ic": summarize_ic dict, "rank_ic": 同左,
#  "layered": {分位: 累计收益 Series}}
```

---

## 6. 研究库 — Brinson 行业归因（`research.attribution`）

将策略相对基准的超额收益分解为**行业配置效应**、**选股效应**、**交互效应**。

### 6.1 `brinson_attribute`（连接数据库）

```python
result = brinson_attribute(
    db_path="result.db",                # 回测结果库（含 trade_log）
    provider_db="/path/to/market.db",   # 行情数据库（只读打开）
    start="20240601",
    end="20240701",
    index_code="000300.SH",             # 基准指数，默认 000300.SH
    run_id=None,                        # 缺省取最新 run
)
```

### 6.2 数据依赖

| 数据 | 行情库表 | 用途 |
|------|---------|------|
| 行业映射 | `index_member_all` | ts_code → 申万 L1 行业（l1_name） |
| 行业收益 | `sw_daily` | L1 行业指数日收益率 |
| 基准权重 | `index_weight` | 基准成分股权重（聚合到行业并归一化） |
| 个股行情 | `stk_factor_pro` | close/pct_chg，用于持仓市值与个股收益 |
| 策略持仓 | 回测库 `trade_log` | 回放重建每日持仓 |

### 6.3 返回结构

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
        "effective_n_industries": ...,   # 有效行业数 = 1/Σ(w²)，按平均权重
        "top3_industries": [{"name": ..., "weight": ...}, ...],
    },
}
```

**数据不足时不抛异常**，返回 `{"error": "原因", "summary": {}, "industry_detail": {}, "daily": [], "exposure_summary": {}}`（如 trade_log 无记录、bars 为空、sw_daily 无数据等），调用方需先检查 `"error"` 键。

**口径（2026-08 版本）**：

- 持仓沿交易日逐日结转——无成交日不丢（买入持有 N 天贡献 N 天归因）；`summary.total_portfolio_return` 是逐日策略收益之和，与回测真实收益在同一量级
- 基准行业权重快照（约每月 2 次）**向前填充到日频**：快照延续至下次调整，首个快照前的日期因无已知基准构成而排除
- 基准收益按**全部基准行业**累加（含策略从未持有的行业）；`portfolio_return` 为全部持仓的市值加权收益，无行业映射/缺 bar 的部分计入 `unexplained`
- `start/end` 与 run 实际区间不一致时告警：持仓自 run 起点重建（期初建仓不被丢失），归因统计按用户区间；数据来自旧版 dump（混指数）的 parquet 结果不可比，请重新导出

### 6.4 `brinson_attribute_from_files`（离线 parquet）

```python
result = brinson_attribute_from_files(
    result_db="backtest_output/run.db",
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

前三个 parquet 用 `scripts/dump_brinson_data.py` 导出（`--index` 指定基准指数）；提供 `--result-db` 与 `--start`/`--end` 时 dump 自动导出 `bars.parquet`，否则 bars 需自行导出。任一文件不存在抛 `FileNotFoundError`。返回值结构与 6.3 相同（含 `"error"` 键约定）。

---

## 7. 典型工作流

### 7.1 因子研究 → 策略 → 回测 → 验证 → 报告

```bash
# 因子初筛：IC / 分层 / 相关性一次看全
python scripts/factor_eval.py mom20,mom_60d,vol_z,ep_z \
    --start 20230101 --end 20240630 --universe CSI300
# 衰减速度（决定调仓频率）
python scripts/factor_eval.py mom20 --start 20230101 --end 20240630 --decay 1,3,5,10,20
# 按 strategy_guide.md 编写策略后：
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out results/v1.db
python scripts/cross_validate.py results/v1.db --strategy my_strategy
python scripts/report.py results/v1.db --out results/v1_report.html
```

### 7.2 参数扫描与多 run 对比

```bash
# 方式一：sweep 一键扫描（参数值列表展开；每组参数是标准 run，可直接 compare）
python scripts/sweep.py sweep.yaml --start 20240101 --end 20240630 --out results/sweep.db
python scripts/compare.py results/sweep.db --html results/sweep_compare.html

# 方式二：手动多次 run 写入同一结果库（适用于策略文件不同的场景）
python scripts/run.py cfg_top5.yaml  --start 20240101 --end 20240630 --out results/sweep.db --no-report
python scripts/run.py cfg_top10.yaml --start 20240101 --end 20240630 --out results/sweep.db --no-report
python scripts/compare.py results/sweep.db --html results/sweep_compare.html
```

### 7.3 Brinson 归因

```python
from research.attribution import brinson_attribute

result = brinson_attribute("results/v1.db", "/path/to/market.db", "20240101", "20240630")
if "error" in result:
    raise RuntimeError(result["error"])

s = result["summary"]
print(f"超额={s['total_excess_return']:.2%} "
      f"(配置={s['allocation_effect']:.2%} 选股={s['selection_effect']:.2%})")

for ind, d in sorted(result["industry_detail"].items(),
                     key=lambda kv: abs(kv[1]["total_contribution"]), reverse=True)[:5]:
    print(f"{ind}: 贡献={d['total_contribution']:.2%} "
          f"(配置={d['allocation_effect']:.2%} 选股={d['selection_effect']:.2%})")

exp = result["exposure_summary"]
print(f"最大单行业: {exp['max_single_industry_name']} "
      f"({exp['max_single_industry_weight']:.1%}), 有效行业数: {exp['effective_n_industries']}")
```

### 7.4 多因子合成研究

```python
from research.composite import combine_factors, evaluate_composite

# factor_df: MultiIndex (trade_date, symbol) 宽表; fwd_ret: 同结构前瞻收益 Series
for method in ["equal", "ic", "icir"]:
    comp = combine_factors(factor_df, fwd_ret, method=method)
    ev = evaluate_composite(comp, fwd_ret)
    print(f"{method}: IC={ev['ic']['ic_mean']:.4f}  IR={ev['ic']['icir']:.2f}")

best = combine_factors(factor_df, fwd_ret, method="icir")  # 供策略使用
```

---

## 8. 速查表

### CLI 命令速查

| 命令 | 作用 |
|------|------|
| `python scripts/run.py <yaml> --start D --end D [--out db]` | 运行回测 |
| `python scripts/factor_eval.py <factors> --start D --end D` | 因子 IC/分层/相关性 |
| `python scripts/report.py <db> [--run-id N] --out x.html` | 单 run 报告 |
| `python scripts/compare.py <db> [--runs 1,2] [--html x.html]` | 多 run 对比 |
| `python scripts/cross_validate.py <db>` | 交叉验证（退出码=问题数） |
| `python scripts/sweep.py <sweep.yaml> --start D --end D` | 参数扫描 |
| `python scripts/replay.py <db> [--symbol S] [--date D]` | 交易决策回放 |
| `python scripts/dump_brinson_data.py <行情库> [--index X] [--result-db db --start D --end E]` | 导出 Brinson 归因数据（单指数基准 + 可选 bars） |

### 研究库 API 速查

| 模块 | 函数 | 输入 → 输出 |
|------|------|------------|
| `research.factor_eval` | `calc_ic(fv, fwd)` | 因子值, 前瞻收益 → (IC Series, RankIC Series) |
| | `summarize_ic(ic)` | IC Series → dict{ic_mean, ic_std, icir, ic_positive_ratio, n_days} |
| | `calc_layered_returns(fv, fwd, n_quantiles)` | → {分位: 累计收益 Series} |
| | `calc_factor_corr(df)` | 因子宽表 → 相关矩阵 |
| | `calc_ic_decay(fv, close_hfq, horizons)` | → horizon × 指标 DataFrame |
| `research.report` | `generate_report(result, path, title?)` | engine.run() 返回 → HTML |
| | `generate_report_from_db(db, path, run_id?)` | → HTML |
| | `generate_compare_report(db, path, run_ids?)` | → HTML |
| | `load_runs(db, run_ids?)` | → [{meta, account_daily, trade_log, statistics}] |
| | `build_compare_table(runs)` | → (header, rows) |
| `research.composite` | `combine_factors(df, fwd, method, window, min_periods)` | → 合成得分 Series |
| | `evaluate_composite(comp, fwd, n_quantiles)` | → {ic, rank_ic, layered} |
| `research.attribution` | `brinson_attribute(db, pdb, start, end, index_code?, run_id?)` | → 归因 dict |
| | `brinson_attribute_from_files(result_db, industry_map, sw_returns, benchmark_weights, bars, run_id?, benchmark_code?)` | → 归因 dict |

### 常见参数速查

| 参数 | 出现位置 | 含义 |
|------|---------|------|
| `--start / --end` | run, factor_eval, sweep, bench | YYYYMMDD 日期范围，必填 |
| `--out` | run, sweep, report, dump_brinson_data | 输出路径（DB / HTML / 目录） |
| `--report` / `--no-report` | run | HTML 报告路径（缺省 auto）/ 关闭报告 |
| `--run-id` | report, cross_validate, replay | 指定 run（均缺省取最新） |
| `--capital` | run, sweep, cross_validate | 初始资金（cross_validate 中用于动态阈值） |
| `--universe` | factor_eval | CSI300/CSI500/CSI1000 或指数代码，默认全市场 |
| `--forward` / `--decay` | factor_eval | 前瞻天数（默认 5）/ IC 衰减模式 |
| `--n-quantiles` | factor_eval | 分层档数，默认 5 |
| `--dry-run` | sweep | 仅打印参数组合 |
| `method` / `window` | combine_factors | equal/ic/icir；IC 滚动窗口默认 60 |
