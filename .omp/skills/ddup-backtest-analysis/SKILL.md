---
name: ddup-backtest-analysis
description: ddup 回测结果深度分析：结果库 schema（trade_log/runs/stats_json）、cross_validate 九项检查与磨损分档阈值、trade_log SQL 六维钻取、statistics 高级键、debug 快照与 replay 回放、Brinson 行业归因与解读。分析回测结果、诊断亏损来源、验证交易合理性、归因与回放异常交易时使用。
---

# ddup 回测结果深度分析

禁止只看 total_return/sharpe 下结论。每个候选策略必须钻到交易明细。API 细节见 `docs/cli_and_research.md`。

## 结果库 schema（多 run SQLite，btcore/database.py）

- `runs(run_id, strategy, start_date, end_date, initial_capital, config_json, status, stats_json)`——status: running/completed/failed；stats_json 含全部统计
- `trade_log(id, run_id, date, symbol, side, trigger, price, shares, turnover, commission, stamp_tax, transfer_fee, slippage_amount, net_amount, reason)`
  - **`net_amount` 对 SELL 行是成交净额（正数），不是盈亏**——用它对卖出行做 SUM/正负判断会得到"全部卖出盈利"的荒谬结论。单笔盈亏只能经 `round_trip`（stats_json 的 trip_detail，含 pnl/pnl_pct/holding_days/sell_trigger）或自行按 BUY/SELL 配对重建。
  - 六维钻取里的胜率列（`SUM(CASE WHEN net_amount>0 ...)`）仅对**配好对的盈亏列**有意义；直接对 trade_log 原始行算胜率应从 stats_json.round_trip 取
- `account_daily(run_id, date, cash, total_value, daily_pnl, cumulative_pnl, n_holdings)`
- `debug_snapshots(run_id, date, snapshot_json)`（仅 debug=True 的 Engine 写入）
- `ml_predictions(run_id, date, symbol, model, score)`（ML 观测，见 ddup-ml-research）
- `sweep_results(id, label, params_json, stats_json)`（sweep.py 附加）
- 注意：holdings 表是瞬态（每次 init 清空），历史持仓从 trade_log/account_daily 重建

trigger 全集：MANUAL（select 名单）、TARGET（target_value 调仓）、CORPORATE（公司行为）、STOP_LOSS/TAKE_PROFIT/TRAILING_TP、ML_EXIT、自定义 handler 类型名。

## 分析顺序

```
1. cross_validate.py 体检（自动九项检查）
2. trade_log SQL 六维钻取
3. statistics 高级键（stats_json）
4. 仍无法解释的异常交易 → debug 重跑 + replay.py
5. 理解超额来源 → Brinson 归因
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

`trading_friction`（total_cost/annualized_cost_drag 年化磨损拖累）、`sell_source`（卖出来源归因）、`symbol_contribution`（个股贡献）、`round_trip.summary.avg_holding_days`、`benchmark_compare`（alpha/beta/IR/tracking_error）、`management_complexity.max_trades_per_day`、`cost_breakdown`。报告与 compare.py 的 11 行指标即源于此。

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
离线复用：`scripts/dump_brinson_data.py` 导 4 个 parquet → `brinson_attribute_from_files(...)`（**注意其 run_id 默认 1 而非最新**）。

## 报告与对比

- 单 run：`python scripts/report.py <db> --out r3.html`（老库自动迁移重算 stats）
- 多 run：`python scripts/compare.py <db> [--runs 1,3,5] --html cmp.html`（<2 run 报错）
