# CLI 命令参考

所有命令从仓库根目录、项目 Python 环境运行；可统一加 `uv run` 前缀。
日期参数用 YYYYMMDD。后端选择见[数据接入](backend_guide.md)，接口语义见各专题指南。

[回测](#run) · [因子评估](#factor-eval) · [报告](#report) · [对比](#compare) ·
[交叉验证](#cross-validate) · [扫描](#sweep) · [回放](#replay) · [归因导出](#brinson-export)

ML 训练参数在 [ML 指南](ml_guide.md#training-cli)；实盘 CLI 参数在
[实盘指南](live_guide.md#workflow)；开发/门禁命令在[开发指南](development.md)。

<a id="run"></a>
## `run.py` — 运行回测

```bash
python scripts/run.py <策略YAML> --start YYYYMMDD --end YYYYMMDD \
    [--capital N] [--out results/run.duckdb] [--report report.html | auto] [--no-report]
```

| 参数 | 说明 |
|------|------|
| `yaml` | 策略 YAML 配置路径（位置参数） |
| `--start` / `--end` | 回测起止日期 YYYYMMDD，**必填** |
| `--capital` | 初始资金 float，覆盖 YAML config |
| `--out` | 结果库 DuckDB 路径；缺省为内存库，不落盘 |
| `--report` | HTML 报告路径。缺省 `auto`：生成到 `<策略目录>/reports/<yaml名>_<起>_<止>.html`（该目录已在 `.gitignore`）；`--report` 裸写（不带值）等价于 `auto` |
| `--no-report` | 关闭报告生成 |

默认使用 adapters.tushare:TushareBackend；设置 DDUP_BACKEND=module:Class 可切换后端。数据库路径由所选后端管理，run.py 没有行情库路径参数。

**输出**：终端打印 statistics 全部统计指标（嵌套分组递归展开）与成交笔数；`--out` 指定时写入结果库；非 `--no-report` 时生成 HTML 报告（见[报告](#report)）。

多 run 表结构与事件口径见[结果分析](results_analysis.md#schema)。同库运行必须串行；
多个进程各用不同输出库，写进程运行时不要持有该库的跨进程读取连接。

```bash
# 最简调用：内存运行 + 自动报告
python scripts/run.py strategies/examples/rolling_ranker/config.yaml \
    --start 20240603 --end 20240628

# 落盘结果库，后续离线生成报告/对比
python scripts/run.py strategies/my_strategy/config.yaml \
    --start 20240101 --end 20240630 --out results/my_run.duckdb --no-report
```

<a id="factor-eval"></a>
## `factor_eval.py` — 因子评估

```bash
python scripts/factor_eval.py <因子列表> --start YYYYMMDD --end YYYYMMDD \
    [--universe CSI300] [--forward N | --decay 1,3,5,10,20] [--n-quantiles N] \
    [--benchmark 000300.SH]
```

| 参数 | 说明 |
|------|------|
| `factors` | 逗号分隔因子名（来自 `factors/library.yaml`），位置参数；使用 `--model` 时可省略 |
| `--factor-library` | 因子库 YAML 路径（缺省 `factors/library.yaml`）；评估策略本地因子库时指定 |
| `--model` | ML 模型 ONNX 路径；模型分数物化为 `ml_<name>` 列后与因子同口径评估（仅支持 panel scope，详见 [ml_guide.md](./ml_guide.md)） |
| `--start` / `--end` | **必填** |
| `--universe` | 股票池：简称 `CSI300`/`CSI500`/`CSI1000`（对应 000300.SH/000905.SH/000852.SH）或其他指数代码（原样透传）；默认全市场。取成分快照并集为候选池，因子面板上再按 **point-in-time 成分**逐日过滤；不能把该过滤等同于引擎物化前逐日裁池 |
| `--forward` | 前瞻收益天数，默认 5 |
| `--exec-price` | close（默认，T 收盘到 T+h 收盘）或 next-open（T+1 开盘到 T+h 收盘） |
| `--decay` | IC 衰减模式，逗号分隔天数（如 `1,3,5,10,20`）；不能与非默认的 `--forward` 同用 |
| `--n-quantiles` | 分层回测档数，默认 5 |
| `--benchmark` | 基准指数代码（如 `000300.SH`）；因子表达式引用 `idx_ret`（市场相对强弱）时必需，口径同引擎（见 [factor_library.md](./factor_library.md)） |

**口径与引擎同源**：

- 因子取值前伸 warmup 窗口（`fplan.main_days`，与引擎 preload 一致），滚动因子在评估窗口头部即有值，不再静默 NaN
- 坍缩因子（市场广度，如 `pct_above_ma20`）走全市场流式 `compute_breadth`，供给窗口由相同机制推导；CLI 输出日频标量，自动跳过截面 IC/分层（group_mean 的组间信息在标量化时合并，见[物化域](factor_library.md#materialization)）；被其他因子表达式引用时会 fail-fast 提示（不支持嵌套坍缩）

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

<a id="report"></a>
## `report.py` — 单 run HTML 报告

```bash
python scripts/report.py <结果库.duckdb> [--run-id N] --out report.html
```

| 参数 | 说明 |
|------|------|
| `db` | 结果库路径，**必填** |
| `--run-id` | 指定 run_id，缺省取最新 run |
| `--out` | HTML 输出路径，**必填** |

输出单文件 HTML（内联 SVG，零依赖，离线可读），章节：核心指标、净值曲线（含基准叠加）、回撤曲线、月度收益、基准对比（Alpha/Beta/信息比率/跟踪误差）、交易磨损与成本拆解、持仓管理复杂度、往返交易汇总、卖出来源归因、个股盈亏贡献 Top10、成交明细。

老 run 无 stats_json 时只读加载并在内存重算，不写回或迁移结果库；缺原始基准/终态持仓信息时，相应指标不能完整恢复。结果库中没有可用 run 时抛 `ValueError`。

<a id="compare"></a>
## `compare.py` — 多 run 对比

```bash
python scripts/compare.py <结果库.duckdb> [--runs 1,2,3] [--html compare.html]
```

| 参数 | 说明 |
|------|------|
| `db` | 结果库路径，**必填** |
| `--runs` | 逗号分隔 run_id 列表，缺省全部 |
| `--html` | 对比 HTML 报告输出路径（可选） |

- 终端打印对比表，指标行：总收益率、年化收益率、夏普比率、最大回撤、Calmar 比率、日胜率、成交笔数、区间换手率、总交易成本、年化磨损拖累、单日最大成交笔数
- `--html` 生成对比报告：元信息表 + 指标对比表 + 归一化净值叠加曲线
- 不足 2 个 run 时报错退出（退出码 1）

<a id="cross-validate"></a>
## `cross_validate.py` — 交叉验证

```bash
python scripts/cross_validate.py <结果库.duckdb> [--run-id N] [--strategy name] [--capital N]
```

| 参数 | 说明 |
|------|------|
| `db_path` | 结果库路径，**必填** |
| `--run-id` | 指定 run_id，缺省取最新 run |
| `--strategy` | 策略名（仅用于输出标注） |
| `--capital` | 初始资金，用于磨损动态阈值；默认 0 = 从 run 的 config 读取，再兜底 1,000,000 |

**退出码 = 发现的问题数**（0 = 通过），可直接用于脚本断言。卖出分类统计的 `total_pnl/win_rate` 来自 `stats_json.round_trip.trip_detail`（按 sell_trigger 聚合，与 `total_realized_pnl` 同源）；无 stats_json 时改列 `net_proceeds`（成交净额，非盈亏）。V1 实盘账本消费路径已随实盘重制移除（docs/live_redesign.md）。

检查项、磨损阈值与解释边界见[结果分析](results_analysis.md#cross-validation)。

<a id="sweep"></a>
## `sweep.py` — 参数扫描批量回测

```bash
python scripts/sweep.py <sweep_config.yaml> --start YYYYMMDD --end YYYYMMDD \
    [--out results/sweep.duckdb] [--capital N] [--dry-run]
```

| 参数 | 说明 |
|------|------|
| `sweep_config` | sweep 配置 YAML，**必填** |
| `--start` / `--end` | **必填** |
| `--out` | 汇总输出数据库，默认 `sweep_result.duckdb` |
| `--capital` | 初始资金，透传给每次 run（覆盖 YAML config） |
| `--dry-run` | 仅打印参数组合，不运行 |

sweep 配置格式——`params` 键为 `.` 分隔的 YAML 嵌套路径，值为候选列表，取笛卡尔积：

```yaml
base: strategies/examples/rolling_ranker/config.yaml
params:
  config.top_k: [3, 5, 10]
  config.max_positions: [5, 10]
```

路径从实际 base YAML 结构出发：顶层 factor_specs 用 `factor_specs.0.weight`；
config 下的字段用 `config.top_k`。base 路径相对当前工作目录；候选值必须为列表，
字符串可能被展开为字符，标量会失败。先 dry-run 检查组合数。

**执行方式**：每组参数在 base 配置上覆写后生成临时 YAML，子进程串行调用 `run.py` 执行（`--no-report` 关闭浪费的 HTML 生成）；失败组合打印 FAIL（保留 stderr 尾部真实异常）并跳过，不中断扫描；全组合失败时非零退出且不打印「结果已保存到」；`params` 为空报错 exit 2。

**输出**：每组参数作为**标准 run** 写入 `--out` 库的 `runs` 表（`config_json` 含参数，`compare.py`/`report.py` 原生可读）；同时写 `sweep_results` 表（`id, label, params_json, stats_json`，stats_json 内含 label 与 params 字段）保留参数标签汇总；终端按 收益/Sharpe/MDD 列打印汇总表。

sweep 内部已关闭报告，不接受 --no-report。临时 YAML 会使相对路径的含义变化：
factor_library 会转绝对路径，models 的 artifact/meta 尚未作同样转换；扫描模型时使用绝对路径。

参数展开逻辑可复用：

```python
from research.sweep import expand_params
combos = expand_params({"config.top_k": [3, 5], "config.max_positions": [5, 10]})
# → [("top_k=3, max_positions=5", {"config.top_k": 3, ...}), ...]
```

<a id="replay"></a>
## `replay.py` — 交易决策回放

从 debug 模式写入的 `debug_snapshots` 表回放每日决策上下文，用于定位某标的在某交易日的买卖依据。

```bash
python scripts/replay.py <结果库.duckdb> [--run-id N] [--symbol SYM] [--date YYYYMMDD] [--list-symbols]
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
python scripts/replay.py results/debug.duckdb --date 20240605 --list-symbols
python scripts/replay.py results/debug.duckdb --symbol 000001.SZ
python scripts/replay.py results/debug.duckdb --symbol 000001.SZ --date 20240605
```

<a id="brinson-export"></a>
## `dump_brinson_data.py` — Brinson 归因数据导出

从行情数据库一次性导出 Brinson 归因所需 parquet，供 `brinson_attribute_from_files()` 离线归因（见[离线归因](results_analysis.md#attribution)）。

```bash
python scripts/dump_brinson_data.py <行情库路径> [--out brinson_data] \
    [--index 000300.SH] [--start YYYYMMDD --end YYYYMMDD] [--result-db 回测库]
```

| 参数 | 说明 |
|------|------|
| `provider_db` | tushare 行情数据库路径，**必填** |
| `--out` | 输出目录，默认 `brinson_data` |
| `--index` | 基准指数代码，默认 `000300.SH`。使用其他基准时显式指定，不能混合多个指数权重 |
| `--start` / `--end` | 日期范围（可选，缺省导出全表） |
| `--result-db` | 回测结果库；提供且同时给 `--start`/`--end` 时自动导出 `bars.parquet`（该库交易过的股票在区间内的 close/pct_chg） |

| 输出文件 | 内容 | 结构 |
|---------|------|------|
| `industry_map.parquet` | 股票→申万 L1 行业 | 列 `ts_code, l1_name` |
| `sw_returns.parquet` | 行业日收益 | index=trade_date，columns=行业名，值为小数（已除以 100） |
| `benchmark_weights.parquet` | 基准行业权重（单指数） | index=trade_date，columns=行业名，每日归一化到和为 1 |
| `bars.parquet` | 个股 bars（--result-db 与 --start/--end 同时提供时导出） | MultiIndex (trade_date, symbol)，列 close/pct_chg |
所有参数也可用 `python scripts/<命令>.py --help` 查实际 argparse；成功退出不证明数据口径或收益结论正确。
