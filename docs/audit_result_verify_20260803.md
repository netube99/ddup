# ddup 回测计算逐条核验记录 2026-08-03

范围：以两个精选策略（champion trend_guard_bw_300 / indmom trend_guard_bw_500_indmom）为载具，
对引擎回测全链路做影子重算逐条核验。方法：引擎侧锚点（debug 快照/trade_log/account_daily/
stats_json/物化面板）+ 影子重算（纯 SQL/pandas，禁止 import btcore）逐字段 diff。
代码版本：cf5f709 + 未提交审计修复（168 行）；真实库 market.db（61.8GB，2026-08-03 03:51 快照）。

## 总览

| 组 | 范围 | 结果 |
|---|---|---|
| 主线程 | 基线/确定性/分叉归因/独立抽查 | 11 项 PASS + 分叉归因完成 |
| V1 数据层 | 复权/涨跌停/ST/亏损/新股/板块价格成分/除权入账/停牌 | 8/8 PASS |
| V2 因子层 | 11 因子影子重算/前视/排名/CSE/breadth/20180905 复现 | 10 PASS + 1 FAIL + 3 WARN |
| V3 决策撮合 | 3 窗口逐笔：价格/触发/涨跌停/股数/费用/净额/T+1/调仓 | 270/270 PASS |
| V4 账户统计 | 账户重建/INV1-8/统计指标/报告/交叉验证/基准 | 27/27 PASS |

## 引擎计算正确性结论（通过项）

- 因子：7 打分因子 + 4 materialize_only + 2 中间列，影子重算 max_diff=0（NaN 对齐）；无前视（截断重算 0 差）；CSE 等价；合成排名 32/32 调仓日与实买一致
- 撮合：3 窗口全部交易逐笔复算价格（手动 open±0.01 / 条件单触发价 3 位小数+滑点 round）、
  涨跌停边界、100 手整、费用（comm=max(t×0.0002,5)/stamp 卖×0.0005/transfer×0.00001）、
  net_amount 恒等式、T+1、11 交易日调仓节奏，0 fail
- 账户：cash 逐笔重建 diff≤5.8e-11；total_value=cash+Σshares×close 一致；INV1-8 全程成立
- 统计：141 项指标（总收益/年化/sharpe/回撤/102 个月收益/round_trip 762+771 笔逐笔/成本归因/
  symbol_contribution 334 标的）全部 1e-9 无差；报告 HTML 5 数字一致且分红信息不缺失
- 过滤：1800 笔买入 ST/亏损(eps<0)/新股(≥60 交易日)/板块/价格/指数成分六规则 0 违规
- 数据：hfq 4396 样本 diff=0；涨跌停 3000 公式复核一致；58 条 DIV/stk_div 端到端金额验证
  （600188 ×1.5 送转、601229/601166 税后 0.8 精确）；3585 笔无停牌日成交
- 引擎不变量：确定性（两次跑逐字段一致）；前视屏蔽（end 不同的全周期 run 重叠区间 1534 行全同）

## 发现清单

| # | 级别 | ID | 现象 | 建议 |
|---|---|---|---|---|
| 1 | P1 | F-EMA-01 | ema_bullish YAML 表达式 `(ema_5>ema_20)+(ema_20>ema_60)+(ema_60>ema_250)` 被 pandas bool 加法 OR 化，列值仅 0/1 而非文档 0-3 分（2018 21,367 行差异）。策略 `float(eb)<3` 恒真 → TREND_BREAK 的 EMA 转空信号永久激活，有效阈值 2→1。101 笔 TB 交易与"实际语义"自洽 | 修 ops 算子 bool 列算术相加，或 YAML 显式 astype，或策略阈值 0；改后须重跑研究基线 |
| 2 | P1 | V4-F1 | round_trip.total_dividend_received 系统性低估现金分红（champ 5121.65 vs 实际 13897.11，-63%；indmom -46%）：FIFO 按 date 稳定排序，同日 SELL(id 早) 先于 DIV，除息日清仓该笔分红整体丢失（13/8 笔）。账户现金/symbol_contribution 精确（gap=0），仅归因指标口径 | stats 排序加 id 次序或 DIV 优先入队 |
| 3 | P2 | F-BRD-02 | compute_breadth 对 group_mean 因子（industry_mom）直接 ValueError：industry 伪列从未附着。docs §16.5"引擎同源"对 group_mean 不成立；测试仅覆盖 mean 型 | compute_breadth 先 ensure_pseudo_columns |
| 4 | P2 | F-BRD-03 | FA-BRD-01 first() 有损量化：223/243 日（91.8%）含多行业组，worst 20181122 返回 0.198 vs 真实 [-0.027, 0.299] | 引擎路径当前不可达（见 #3），修 #3 时一并修 |
| 5 | P3 | F-OP-04 | plan._project 硬编码 industry 组键；本路径 benign（主面板 industry NaN=0），广度面板 6,917 行 NaN，其他组键会静默错误 | 组键参数化 |
| 6 | P3 | F-EX-02 | where 两路径 NaN 语义：两策略实际闭包暴露面 0 行（全为比较型 where） | 归零，可关闭 |
| 7 | P3 | F-MATCH-05 | order_volume_ratio=None → cap_by_volume 未启用；120 笔买入股数与等权重建完全匹配 | 观察 |
| 8 | P2 | 数据源 | bak_basic 数据洞日：20200803 全表缺行 + 20191128/20200720/20200804/20210129 部分缺行（9 笔买入走 pe_ttm 回退，均>0 未误杀）；600837.SH 无 stock_basic 行（新股判定退化）；stk_limit 上市首日哨兵 999999.999/0.01（001379.SZ）；adj_factor 与 dividend 现金口径差 0.006 元/股（000001.SZ 20240614） | 重新同步对应表 |
| 9 | P3 | F-DATA-01 | market.db 无 2017 数据 → 2018 窗口 warmup 空、seed select 无 pending、首买 20180103 | 需 2017 数据才可前伸 |

## 重大口径事实：R24 交付数字基于旧代码

分叉归因（全周期 champion，20180905 起 485 个分叉日）：

| 版本 | 全周期收益 | 说明 |
|---|---|---|
| 旧代码（cf5f709，verify 库 2026-08-02） | +253.98% | R24 记录 +254.0% 的来源 |
| 新代码关新股过滤 | +350.80% | 单变量：F-NEW-01 影响 +36pp |
| 新代码全开（至 20260630） | +372.06% | D-DIV-02 等影响 +97pp |
| 新代码全开（至 20260731） | +386.85% | 7 月 +3.13% |

- 机制实证：旧代码新股 cutoff 锚定 end（[end-60,end]），全周期窗口内早段次新股全漏滤，
  污染 z-score 截面 → 20180905 调仓 601328 vs 600048（刀刃级 0.0005，4 只次新股
  001965/002925/601828/601838）。影子排名精确复现两引擎实买。
- 旧代码 2018H2 与 verify 库 102 笔全同（stash 实验铁证）；2024H1 两版本一致（无相关次新股/分红事件）。
- **结论：R24 交付数字（champion +254%、indmom +241% 等）作废，需用新代码重跑研究基线。**

## 遗留（未修，待用户决策）

1. F-EMA-01 修复方向（算子/YAML/策略）——影响策略行为与研究基线，需用户拍板
2. V4-F1 归因口径修复（stats round_trip FIFO 排序）
3. F-BRD-02/03 修复（compute_breadth 伪列附着 + first() 替换）
4. 数据洞日重新同步（bak_basic/stock_basic/stk_limit 哨兵）
5. 未提交审计修复（168 行）建议尽快提交，避免后续研究继续跑在混合口径上

## 产物

- 引擎锚点：/tmp/verify/{champ,indmom}_{20180101_20180630,20240101_20240630,20260101_20260630}.db（debug）、
  champ_full.db / indmom_full.db（全周期）、/tmp/verify/panels/*.parquet（因子面板）
- 各组输出：/tmp/verify/out_V1.json / out_V2.json / out_V3.json / out_V4.json
- 影子脚本：/tmp/verify/v1_verify.py / shadow_factors.py / v3_shadow.py 等

## 修复后基线（2026-08-03，提交 c2d5327 + 12c4413）

两处 P1 修复已落地（F-EMA-01 bool 加法算术化、V4-F1 round_trip 同日分红优先归因），
新代码重跑全部窗口（results/verify/v2/，17 半年窗 + 2 全周期 × 2 策略）：

| 策略 | 窗口 | 新代码（v2） | 旧代码（R24） |
|---|---|---|---|
| champion | 2018-2026 全周期 | +332.7% | +254.0% |
| champion | 2022-2026 | +137.8% | — |
| champion | 2026H1 | +16.66% | +16.0%* |
| indmom | 2018-2026 全周期 | +145.4% | +241.1% |
| indmom | 2022-2026 | +200.6% | — |
| indmom | 2026H1 | +36.37% | +39.95% |

*R24 记录口径。ema_bullish 修复使 TREND_BREAK 恢复设计阈值（2 信号），交易行为显著改变，
全周期绝对数字不可与旧代码直接比较；R23 相对关系保持（indmom 2026H1 仍跑赢 champion ~20pp）。
研究状态见 results/research_state.yaml R25。
