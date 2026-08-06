---
name: ddup-backtest-analysis
description: ddup 回测结果深度分析：结果库 schema（trade_log/runs/stats_json）、cross_validate 九项检查与磨损分档阈值、trade_log SQL 六维钻取、statistics 高级键、debug 快照与 replay 回放、Brinson 行业归因与解读。分析回测结果、诊断亏损来源、验证交易合理性、归因与回放异常交易时使用。
---

# ddup 回测结果深度分析

禁止只看 total_return/sharpe 下结论。每个候选策略必须钻到交易明细。API 细节见 `docs/cli_and_research.md`。

## 结果库 schema（多 run SQLite，btcore/database.py）

- `runs(run_id, strategy, start_date, end_date, initial_capital, config_json, status, stats_json, created_at)`——status: running/completed/failed；stats_json 含全部统计
- `trade_log(id, run_id, date, symbol, side, trigger, price, shares, turnover, commission, stamp_tax, transfer_fee, slippage_amount, net_amount, reason)`
  - 公司行为行（勿当交易计数）：`DIV`（现金分红，shares=0，net_amount=税后净分红）/ `STK_DIV`（送转，shares=送转后总股数，net_amount=0，trigger=CORPORATE）；实盘账本衍生库另有 `ADJUST` 审计行（side/trigger=ADJUST，symbol="", net_amount=现金调整）
  - **`net_amount` 对 SELL 行是成交净额（正数），不是盈亏**——用它对卖出行做 SUM/正负判断会得到"全部卖出盈利"的荒谬结论。单笔盈亏只能经 `round_trip`（stats_json 的 trip_detail，含 pnl/pnl_pct/holding_days/sell_trigger）或自行按 BUY/SELL 配对重建；两者同口径：**pnl = 卖出净额 − 买入含费成本 + 分红（含费用，与 symbol_contribution/ML 标签一致，2026-08 统一 CONS-01）**，同日 DIV/STK_DIV 先于买卖归入在持回合。
  - 六维钻取里的胜率列（`SUM(CASE WHEN net_amount>0 ...)`）仅对**配好对的盈亏列**有意义；直接对 trade_log 原始行算胜率应从 stats_json.round_trip 取
- `account_daily(run_id, date, cash, total_value, daily_pnl, cumulative_pnl, n_holdings, initial_capital)`——**stats 以首日 initial_capital 为收益分母**（btcore/stats.py）
- `debug_snapshots(run_id, date, snapshot_json)`（仅 debug=True 的 Engine 写入）
- `ml_predictions(run_id, date, symbol, model, score)`（ML 观测，见 ddup-ml-research）
- `sweep_results(id, label, params_json, stats_json)`（sweep.py 附加）
- 注意：holdings 表是**终态快照**（引擎只在 run 收尾写一次，运行期不逐日写），历史持仓须从 trade_log/account_daily 重建

trigger 全集：MANUAL（select 名单）、TARGET（target_value 调仓）、CORPORATE（公司行为）、STOP_LOSS/TAKE_PROFIT/TRAILING_TP、ML_EXIT、自定义 handler 类型名；实盘账本衍生库另有 ADJUST（现金审计行）。

## 分析顺序

```
1. cross_validate.py 体检（自动九项检查）
2. trade_log SQL 六维钻取
3. statistics 高级键（stats_json）
4. 仍无法解释的异常交易 → debug 重跑 + replay.py
5. 理解超额来源 → Brinson 归因
6. 数字在代码/数据变动后异常变化 → 结果变化归因（§6）
7. 不信任引擎输出 → 影子重算核验（§7）
```

## 1. cross_validate.py（每个 run 必做）

```bash
python scripts/cross_validate.py results/r3.db [--run-id N]   # 退出码=问题数，0=通过
```
九项检查：trigger 分布（集外仅 INFO）、买卖比>3 或 <0.3、同日同票买卖冲突、**交易磨损/资金比超分档阈值**（≤5万 3%、≤50万 1%、>50万 0.5%，另加最低佣金×2+印花税底；≤5万降级 INFO）、小单过多（≥10万资金且>50% 成交 <25000）、日均成交>10 笔、持仓超 max_positions、负现金、卖出按 trigger 分类统计（INFO）。

## 2. trade_log SQL 六维

```sql
-- ① 卖出按 trigger 分组：哪个 trigger 在亏钱？胜率？
SELECT trigger, COUNT(*), AVG(net_amount), SUM(CASE WHEN net_amount>0 THEN 1 ELSE 0 END)*1.0/COUNT(*)
FROM trade_log WHERE side='SELL' AND run_id=? GROUP BY trigger;
-- ② 个股盈亏 TOP/BOTTOM：单票集中风险？亏损票反复买？
-- ③ 同票买入次数：反复买同一只亏损股的模式
-- ④ 持仓时长分布（entry→exit）：太短=入场门槛不严
-- ⑤ account_daily 月度收益：连续亏损月？最大单月亏损？
-- ⑥ 成本结构：commission+stamp_tax+slippage_amount 占资金比 vs 分档阈值（见①）
```
大表提取派子 agent 做，主线只看聚合结论。

## 3. statistics 高级键（runs.stats_json 或 Engine.run() 返回）

`trading_friction`（total_cost/annualized_cost_drag 年化磨损拖累）、`sell_source`（卖出来源归因）、`symbol_contribution`（个股贡献）、`round_trip.summary.avg_holding_days`、`benchmark_compare`（alpha/beta/information_ratio/tracking_error）、`management_complexity.max_trades_per_day`、`cost_breakdown`、`total_dividend_received`（已实现分红）/ `total_dividend_accrued`（+期末未平仓 lot 未实现分红，全口径）、`max_dd_unrecovered`（回撤未修复时 True，此时 `max_drawdown_recovery_days` 保留旧值含谷值日）。报告与 compare.py 的 11 行指标即源于此。

## 4. debug 回放（SQL 发现异常但无法解释时）

run.py **没有 --debug 开关**，快照只能代码路径写：

```python
engine = Engine(strategy, provider, debug=True, db_path="results/debug.db")
engine.run("20240101", "20240630")
```
```bash
python scripts/replay.py results/debug.db --symbol 000001.SZ --date 20240605
python scripts/replay.py results/debug.db --date 20240315 --list-symbols
```
缺省 run_id 取最新 run；库中无 run 记录（**含旧库无 runs 表**）一律报错并以退出码 1 退出。
快照含当日账户状态、pending buy/sell/buy_conditions、每持仓明细+最多 5 个因子列。逐日回答"那天为什么买/卖这只"。

## 5. Brinson 行业归因（理解超额来源）

```python
from research.attribution import brinson_attribute
r = brinson_attribute("results/r3.db", provider_db,   # 行情库路径：用后端的默认库路径 API 获取
                      "20240101", "20250630", index_code="000300.SH", run_id=None)  # None=最新 run
# r["summary"]: total_excess_return 分解为 allocation/selection/interaction_effect + unexplained
# r["industry_detail"]: 每行业 active_weight、各效应、total_contribution
```
解读：**配置效应大** → 超额靠押行业 → 方向转行业轮动/exclude_industries；**选股效应大** → 靠个股选择 → 继续深挖因子。数据不足不抛异常，返回 {"error": 原因}——先检查 error 键。
离线复用：`scripts/dump_brinson_data.py` 默认导 3 个 parquet（industry_map/sw_returns/benchmark_weights）；**bars.parquet 需同时给 `--result-db` 与 `--start`/`--end` 才导出**（缺它 `brinson_attribute_from_files` 抛 FileNotFoundError）。`brinson_attribute_from_files(...)` 的 run_id 默认 1 而非最新。

## 6. 结果变化归因（回测数字变了先归因再定论）

同一策略结果在新旧代码/配置/数据下大幅变化时（引擎修复、参数调整、数据更新），按序执行：

1. **锁基线**：`git rev-parse HEAD` 短哈希 + `git diff` 统计（未提交修改是头号嫌疑）；行情库表行数 + mtime
2. **二分定位**：`git stash push` 单文件/文件组跑对照，或不动代码用 YAML 关功能（如 `exclude_new_stock: false`）；旧代码+旧结果逐位相同 = 该文件是根因
3. **量化单变量贡献**：全周期跑关/开对比，差异即该修复贡献
4. **分年收益定位**：account_daily 按年首末日 total_value 比值，找差异最大年份
5. **交易构成对比**：`SELECT reason, COUNT(*) FROM trade_log GROUP BY reason`——某 trigger 触发频率大变（如 TREND_BREAK -11%）是行为改变指纹
6. **样本实证**：同 symbol 同波持仓在新旧库的卖出日/价格 set 差集对比

关键判断：

- **正确性修复 vs 行为语义修复**：数据/过滤/分红类修复让收益升（旧计算错）；因子语义修复（bool 加法等）让收益变（回归设计意图），升降都可能
- **复利放大**：单笔 1-3% 的小差异经多年复利可放大到几十到上百 pp，不要因单笔小就忽略
- **相对结论仍有效**：历史轮次实验在同一 bug 行为下进行时，轮次间相对比较不受影响；只有绝对数字和交付声明作废
- **修复后变差 ≠ 修错了**：策略 alpha 可能依赖 bug 行为（如快切阈值实际 1）；可选 A/B 把语义参数化，属策略决策

## 7. 引擎结果核验（不信任引擎输出时的影子重算）

pytest 绿不算证据。引擎所有计算默认不可信时的核验路径：

- **确定性验证先行**：同配置跑两次 debug，trade_log/account_daily/debug_snapshots 逐字段零差异——否则一切 diff 无意义
- **锚点导出**：`Engine(..., debug=True)` → debug_snapshots 表（每日 pending/bars/holdings/account 内部状态）+ trade_log + account_daily
- **影子重算**：手写 pandas+sqlite3 从行情库原始表独立重算（**禁止 import btcore**，按 factors/library.yaml + docs 公式实现——文档与实现漂移本身就是核查对象），与引擎锚点逐字段 diff
- **三方闭环**：stats_json ↔ trade_log 重建 ↔ account_daily 重建完全一致
- **核心原则**：成交的票必须通过全部过滤器（一笔违规买入即 bug）；成交价=裸价±滑点、因子=复权口径
- 完整 7 层 40 项清单见 `docs/audit_checklist.md`；核验后数字变化 → 走 §6 归因

## 报告与对比

- 单 run：`python scripts/report.py <db> --out r3.html`（老库自动迁移重算 stats）
- 多 run：`python scripts/compare.py <db> [--runs 1,3,5] --html cmp.html`（<2 run 报错）
