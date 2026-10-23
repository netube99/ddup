# 实盘账本与每日操作单

[文档入口](index.md) · [结果分析](results_analysis.md) · [架构](../ARCHITECTURE.md#ml-live-design)

账本记录真实成交，策略通过回放生成次日建议。本工具不连接券商自动下单。
ledger_fills 是唯一手工成交源；现金、持股、公司行为和报告表由回放衍生。
换策略不改历史成交，但可能改变建议。当前行情链支持 A 股个股，ETF/基金不在支持域。

<a id="workflow"></a>
<a id="commands"></a>
## 每日流程与写入边界

1. 更新行情、交易日历和分红数据，确认已覆盖对账日。
2. 从券商取得现金、全量持仓和成交明细，执行 sync。持股不一致整体回滚。
3. 对账通过后运行 signal，读取次日操作单；它会重写账本的衍生表，不是只读查询。
4. 由使用者按实际账户和券商规则执行建议；下一次 sync 录入实际成交。

```bash
python scripts/live.py init live/main.duckdb --date 20260731 --cash 40000
python scripts/live.py sync live/main.duckdb sync.yaml
python scripts/live.py signal live/main.duckdb strategies/my_strategy/config.yaml \
  --date 20260803 --out live/opsheet.json
python scripts/live.py status live/main.duckdb
```

init/sync/signal 都会写账。研究、排错、换策略比较在副本执行；真实写账需属于用户
明确授权的操作。不要在真实账本运行 E2E 脚本。status 用于读取现有状态，非建账入口。

<a id="input"></a>
## 建账与对账输入

init 的 --date 必须是开市日。已有持仓通过 --positions 传入列表：

```yaml
- {symbol: 000001.SZ, shares: 100, entry_date: "20260730", entry_price: 12.0}
```

entry_date/entry_price 用于重建持有天数与价格锚点，写成 OPENING 记录。
账本 schema_version=1；兼容无版本键的旧账本，拒绝未知版本。

sync.yaml：

```yaml
date: "20260803"
cash: 41233.55
holdings:                         # 券商全量持仓；空仓为 []
  - {symbol: 600519.SH, shares: 100}
fills:                            # 实际成交，可为 []
  - symbol: 000001.SZ
    side: SELL
    price: 12.34
    shares: 1000
    commission: 2.47
    stamp_tax: 6.17
    transfer_fee: 0.0
    reason: TREND_BREAK
```

示例金额/持仓仅示意结构，必须替换成一致的真实账户数据。

| 规则 | 实际行为 |
| --- | --- |
| statement 日期 | 非开市日归一到此前最近开市日；晚于行情末日拒绝 |
| fill 日期 | 缺省用归一后的 statement 日期；显式日期须在账本起点至对账日的交易日历内 |
| side | sync 仅接受 BUY/SELL/ADJUST；OPENING 仅用于 init |
| 数值 | 拒绝 NaN/Inf、负券商现金、非法股数；股数须为整数 |
| 对账 | 追加成交后回放并逐只比较持仓，差异则事务回滚 |
| 现金差额 | 记 ADJUST；小于 0.01 忽略，大于 100 元告警，需解释出入金或漏费 |
| 幂等 | 完全重复成交跳过，含批内重复及 side 大小写；落库保留原始价格/费用精度 |
| reason | 与回测 trigger 对应，供冷却期、卖出归因、ML 标签使用 |

DIV/STK_DIV 从分红表自动衍生，不手工录入成交。不得直接改衍生持仓来凑平对账；
修正输入后重跑，已经入账的历史错误需先定位并制定可追溯修复。

<a id="operations"></a>
## signal 输出与持久化

| 字段 | 使用方式 |
| --- | --- |
| open_sells | 次日开盘卖出建议，保留 reason；包括盘前趋势退出 |
| open_buys | T 收盘价估算股数，实际开盘价格/资金/限制可能使成交不同 |
| broker_conditions | 持仓止盈/移动止盈/止损触发价，按券商能力设置当日有效条件 |
| notices | 除权预告、停牌等提示；结合当日实际市场状态判断 |

同票多条件需核对优先级和成交后撤单机制。程序内“首条触发”不保证券商多单的执行顺序。
当日条件需每日重新核对设置；未成交不等于应无限顺延。

signal 全量回放并重写 runs/trade_log/account_daily/holdings 等衍生数据，结果可由
report/cross_validate/replay 消费。ledger_fills 保留；不要把衍生 run_id 当稳定历史标识。
账本状态读取还使用 ledger_holdings。行情缺口和股票池外持仓要检查补价面板，
不能把缺行情解释成持仓已消失。

回放窗口内即使加载股票池某个交易日整日无 bar，公司行为和当日账本成交仍按交易日处理；
持仓交易日计数与解锁照常推进，并清空该日 pending 和持仓条件单。该日不调用策略决策钩子；
恢复行情后策略按更新后的持仓状态继续生成建议。窗口末日仍须有行情，否则 signal 报错。

<a id="troubleshooting"></a>
## 排错顺序

| 输出/现象 | 下一步 |
| --- | --- |
| parse_error | 按 message 修正 YAML 类型、字段和有限数值 |
| invalid_fill_date | 按 problems 查成交日期，不用 statement 归一掩盖错误 |
| no_market_data | 更新行情，核对 statement 日期与最后交易日 |
| bad_db | 核对账本路径、初始化和版本；不要误用回测结果库 |
| data_error | 查重复成交、无买入先卖出、非法 OPENING 等历史矛盾 |
| holding_diffs | 对照券商逐笔成交和公司行为；在副本复现，再修正来源 |
| signal 无行情/非交易日 | 检查日期、个股支持范围和数据覆盖 |
| signal 路径错误 | 检查策略 YAML 与 --out 父目录 |
| 换策略后资产变化 | 核查股票池外持仓补价与缺行情，独立重建估值 |

sync 的业务错误通过 {ok:false, stage, message} 返回；不能只看是否产生 JSON。
status 对不存在/未初始化账本报错，不静默建库。现金 ADJUST 告警不是自动证明对账合理。

<a id="verification"></a>
## 回归与实盘验收

fixtures 测试不需要真实数据库。真实回放脚本需要已存在的回测库：

```bash
python scripts/live_e2e_check.py --bt-db results/baseline.duckdb \
  --ledger /tmp/ddup-live-check/test.duckdb --yaml strategies/my_strategy/config.yaml
```

--bt-db 必填。--ledger 必须位于本次专用空目录：脚本启动会删除该路径和同目录
的 test_b.duckdb。先创建专用目录，绝不传真实账本或含重要账本的目录。
它检查 init→sync→signal 与特定回测 run 的操作单一致性、拒绝坏 statement、
中途建账等价性；所选窗口末日须无待执行开盘订单，且适用其持仓重建假设。
这个检查证明选定样本的回放一致，不能替代券商现金/持股快照核验。

报告分别写自动测试、回放抽样、券商对账证据。没有券商快照则实盘一致性写“未验证”。
