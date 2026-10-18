---
name: ddup-backtest-analysis
description: 分析 ddup 回测结果库、交易异常、费用和收益来源，进行 SQL 钻取、回放、独立核算与 Brinson 归因。
---

# 解释回测结果

先固定结果库、strategy/run_id、完成状态、窗口、初始资金、模型与数据版本。
阅读[结果表语义](../../../docs/results_analysis.md#schema)，不要混合不同 run。

## 分析路径

1. 核对 account_daily 与 stats_json 的统计口径；缺历史字段时区分原始值与报告内存重算。
2. 执行[cross_validate](../../../docs/results_analysis.md#cross-validation)，逐条区分
   预期拒单、数据缺口、计算错误与策略经济性。退出码 0 不证明会计或前视正确。
3. 按[事件/SQL 口径](../../../docs/results_analysis.md#events)从日期、股票、买卖方向、
   trigger、费用、持有回合钻取。先看贡献集中和异常样本，再推断整体原因。
4. 可疑交易用[debug/replay](../../../docs/results_analysis.md#debug)还原决策与次日执行，
   用原始价格/费用独立重算；无快照时明确证据缺口。
5. 行业集中或超额来源问题读[Brinson](../../../docs/results_analysis.md#attribution)，
   先核对 error、数据覆盖和 unexplained，再解释配置/选股/交互效应。

## 易误读项

- SELL net_amount 是卖出所得，不是利润；price 已含滑点，turnover 是滑点前名义成交额。
- STK_DIV shares 是新总股数，不是新增股数；DIV 是税后现金，需计入持有回合。
- holdings 是最新成功 run 的终态，不按 run_id 留历史，也不是日持仓表。
- 统计含初始资金与首日收益/回撤基准；归因逐日求和不能直接等同回测复利总收益。
- debug 的 pending 是下一日订单；holding ML 分数查 ml_predictions，不能假设原面板快照里存在。
- 摩擦阈值按资金分档，只是行为筛查；高换手、亏损或复杂管理不等于引擎算错。

交付支持结论的 run/SQL/样本、独立期望、实际差异与未验证项。
确认错误只报告其证据和影响；修复请求遵循[审查协议](../../../docs/review_protocol.md)，
不借分析机会自动重构或调参数。
