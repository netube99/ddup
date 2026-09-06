---
name: ddup-live-ops
description: ddup 实盘账本每日操作流程与故障排查：数据更新 → sync 全量对账 → signal 明日操作单 → 券商条件单设置；对账拒绝/数据错误的根因定位与恢复；live_e2e_check.py 回归。维护实盘账本、执行每日实盘流程、排查对账不一致时使用。
---

# ddup 实盘每日操作（账本驱动，策略零耦合）

研究收官 → 部署：**账本（ledger）是唯一持久化状态，与策略完全解耦**——换任意策略 YAML 重放即得该策略口径的操作单。API 细节见 `docs/cli_and_research.md` §2.9。

## 每日节奏（收盘后，券商数据落地后）

1. **更新行情库**（日线含 adj_factor；数据未更新时 signal 报"无行情数据"）
2. **sync**：`python scripts/live.py sync live/main.db sync.yaml` — 全量账户信息一次性给到位
3. **signal**：`python scripts/live.py signal live/main.db strategies/selected/<cfg>/config.yaml [--date D] [--out opsheet.json]`
4. **盘前执行**：open_sells/open_buys 开盘手动单；broker_conditions 设券商条件单（触发价精确、当日有效、每日重设）；同票多单首触后撤其余

sync.yaml 格式：`date/cash/holdings[{symbol,shares}]/fills[{symbol,side,price,shares,commission,stamp_tax,transfer_fee,reason}]`（全量账户信息一次性给到位，可为空 fills）。

## 子命令

- `init`：`live.py init live/main.db --date 20260731 --cash 40000 [--positions p.yaml]` 建账——positions 每条 `{symbol, shares, entry_date, entry_price}`，以 `OPENING` 条目入账（entry_date/entry_price 用于 holding_days 与 trailing 锚点重建）；缺省空仓开局
- `sync`：每日对账——追加今日成交 → 轻量回放（无因子，秒级）→ 衍生持仓与券商逐只比对，**不一致即回滚并报差异**；现金差额自动记 `ADJUST`（<0.01 忽略；>100 元 warning，可能是出入金/漏录费用）
- `signal`：全量回放 → 明日操作单 JSON：`open_sells`（含 reason）/ `open_buys`（T 收盘预估股数，实际以明日开盘价定）/ `broker_conditions`（每只持仓 TAKE_PROFIT/TRAILING_TP/STOP_LOSS 精确触发价，盘前设置当日有效）/ `notices`（除权预告、停牌、T+1 锁定）；衍生表（runs/trade_log/account_daily/holdings）整体重写，**与回测结果库同 schema**——report/cross_validate/replay 直接可用
- `status`：最近一日 account_daily、持仓快照（ledger_holdings）、最近 10 条成交

## 账本语义（不可违背）

- `ledger_fills` append-only 唯一真相源；**持仓/现金永远衍生，不可手改**
- 公司行为（DIV/STK_DIV）回放时从分红表自动衍生，**不要手工录入**
- fills 幂等：完全重复的成交自动跳过（append_fills_idempotent），重跑同一 statement 安全
- reason 字段 = 回测 trigger：TREND_BREAK（盘前评估离场）、条件单触发如实记（TRAILING_TP 等）、手动操作 MANUAL——冷却期记账与 ML 标签都消费它

## 故障排查

| 现象 | 根因 | 处理 |
|---|---|---|
| sync `ok:false` + holding_diffs | 漏录/错录成交（statement 与账本衍生持仓股数不一致） | 找缺失的成交补录后重跑；**禁止**手改持仓 |
| sync `stage:data_error` | 账本数据自相矛盾（重复入账/卖出无买入记录） | 检查 ledger_fills 是否被重复 append；回滚已自动执行，直接修正后重跑 |
| signal 报"无行情数据" | 行情库未更新到 D | 先更新数据再 signal |
| signal 报"不是交易日" | D 非交易日 | 用最近交易日 |
| 同一账本换策略 signal 后 total_value 变化 | universe 外持仓估值缺口 | 已修复（build_price_fallback 补价面板），若复发检查 fallback |

## 实盘 vs 回测口径差异

- TREND_BREAK = 盘前评估（T 收盘指标 → T+1 开盘卖），走 open_sells 带 reason
- TAKE_PROFIT/TRAILING_TP/STOP_LOSS = 盘中价格触发 → broker_conditions 监控表，不进 open_sells
- 买入股数只能预估（T 收盘价口径），实际以次日开盘定
- 涨跌停/停牌在实盘当场可见，次日 sync 自动吸收偏差

## 回归验证

引擎/机制改动后跑 `python scripts/live_e2e_check.py [--bt-db 回测库] [--ledger 账本]`：以回测 result.db 为 ground truth 模拟 23 个交易日（init → 每日 sync → 每日 signal），操作单必须与回测次日实际成交**逐符号逐 reason 一致**；坏 statement 拒绝、中途建账播种与全程回放等价。
