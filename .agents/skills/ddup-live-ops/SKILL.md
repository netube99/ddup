---
name: ddup-live-ops
description: ddup 实盘账本每日操作流程与故障排查：数据更新 → sync 全量对账 → signal 明日操作单 → 券商条件单设置；对账拒绝/数据错误的根因定位与恢复；live_e2e_check.py 回归。维护实盘账本、执行每日实盘流程、排查对账不一致时使用。
---

# ddup 实盘每日操作（账本驱动，策略零耦合）

研究收官 → 部署：**账本（ledger）是唯一持久化状态，与策略完全解耦**——换任意策略 YAML 重放即得该策略口径的操作单。API 细节见 `docs/cli_and_research.md` §2.9。

## 每日节奏（收盘后，券商数据落地后）

1. **更新行情库**（日线含 adj_factor；数据未更新时 signal 报"无行情数据"）
2. **sync**：`python scripts/live.py sync live/main.duckdb sync.yaml` — 全量账户信息一次性给到位
3. **signal**：`python scripts/live.py signal live/main.duckdb strategies/selected/<cfg>/config.yaml [--date D] [--out opsheet.json]`
4. **盘前执行**：open_sells/open_buys 开盘手动单；broker_conditions 设券商条件单（触发价精确、当日有效、每日重设）；同票多单首触后撤其余

sync.yaml 格式：`date/cash/holdings[{symbol,shares}]/fills[{symbol,side,price,shares,commission,stamp_tax,transfer_fee,reason}]`（全量账户信息一次性给到位，可为空 fills）。

## 子命令

- `init`：`live.py init live/main.duckdb --date 20260731 --cash 40000 [--positions p.yaml]` 建账——`--date` 必须为 YYYYMMDD 且是开市日（用行情日历校验，非开市日 fail-fast 并提示最近开市日）；positions 每条 `{symbol, shares, entry_date, entry_price}`，以 `OPENING` 条目入账（entry_date/entry_price 用于 holding_days 与 trailing 锚点重建）；缺省空仓开局。写入 `schema_version=1`；打开旧账本缺该键兼容，未知版本报错
- `sync`：每日对账——先校验日期再落库：statement date 非开市日自动归一到 ≤date 最近开市日，且该日必须有行情数据（晚于数据最后一天 fail-fast）；每个 fill 的显式日期（缺省用归一化后的 statement date）必须落在 `[账本 start_date, 对账日]` 交易日历内，否则**整体拒绝不落库**（杜绝回放永不生效的死行）；fills 仅接受 BUY/SELL/ADJUST（OPENING 仅 init 用）。通过后追加今日成交 → 轻量回放（无因子，秒级）→ 衍生持仓与券商逐只比对，**不一致即回滚并报差异**；现金差额自动记 `ADJUST`（<0.01 忽略；>100 元 warning，可能是出入金/漏录费用），券商现金为负拒绝入账；cash/price/shares/fees 拒绝 NaN/Inf。解析/数值/日期错误统一 JSON 错误通道 `{"ok": false, "stage": ...}`，重跑幂等
- `signal`：全量回放 → 明日操作单 JSON：`open_sells`（含 reason）/ `open_buys`（T 收盘预估股数，实际以明日开盘价定）/ `broker_conditions`（每只持仓 TAKE_PROFIT/TRAILING_TP/STOP_LOSS 精确触发价，盘前设置当日有效）/ `notices`（除权预告、停牌；universe 外持仓按补价面板判定，不再误报停牌）。业务错误（YAML 不存在、`--out` 目录不存在、非交易日、无行情数据）打可读消息返回非 0，不再裸 traceback；衍生表（runs/trade_log/account_daily/holdings）整体重写，**与回测结果库同 schema**——report/cross_validate/replay 直接可用
- `status`：最近一日 account_daily、持仓快照（`ledger_holdings`）、最近 10 条成交；账本库不存在或未初始化 → 报错 exit 2，不静默建库

## 账本语义（不可违背）

- `ledger_fills` append-only 唯一真相源；**持仓/现金永远衍生，不可手改**
- 公司行为（DIV/STK_DIV）回放时从分红表自动衍生，**不要手工录入**
- fills 幂等：完全重复的成交自动跳过（append_fills_idempotent，含同批重复与 side 大小写），重跑同一 statement 安全；归一化只用于判重，落库保留原始 price/fees 精度，shares 必须为整数
- 非开市 statement date 会归一到上一开市日再落账；fill 显式日期必须本身是交易日且在账本区间内，不能靠 statement 级归一化兜底
- reason 字段 = 回测 trigger：TREND_BREAK（盘前评估离场）、条件单触发如实记（TRAILING_TP 等）、手动操作 MANUAL——冷却期记账与 ML 标签都消费它

## 已知限制

- **仅支持个股**：行情链只覆盖 A 股个股；账本若持有 ETF/基金标的则无行情 → `signal` 报「无行情数据」、`ledger_holdings` 不刷新（`sync` 对账仍可用）。ETF 持仓请自行控制或清仓

## 故障排查

| 现象 | 根因 | 处理 |
|---|---|---|
| sync `ok:false` + holding_diffs | 漏录/错录成交（statement 与账本衍生持仓股数不一致） | 找缺失的成交补录后重跑；**禁止**手改持仓 |
| sync `stage:data_error` | 账本数据自相矛盾（重复入账/卖出无买入记录/OPENING 混入 fills） | 检查 ledger_fills；回滚已自动执行，直接修正后重跑 |
| sync `stage:parse_error` | sync.yaml 读不了/字段非法（cash 非数、NaN、holdings 缺 shares、现金为负） | 按 message 修正 YAML 字段 |
| sync `stage:invalid_fill_date` | fill 日期非交易日或早于账本起始日 | 按 `problems` 列出的行号改 fill 的 date（或删掉误录行）后重跑 |
| sync `stage:no_market_data` | statement date 晚于行情库最后一天 | 先更新行情数据再 sync |
| sync/status `stage:bad_db` | 账本库不存在/未初始化/schema 版本未知 | 确认路径正确；新账本先 init |
| signal 报"无行情数据" | 行情库未更新到 D | 先更新数据再 signal |
| signal 报"不是交易日" | D 非交易日 | 用最近交易日 |
| signal 报"YAML 不存在/--out 目录不存在" | 参数错误 | 修正路径 |
| 同一账本换策略 signal 后 total_value 变化 | universe 外持仓估值缺口 | 已修复（build_price_fallback 补价面板），若复发检查 fallback |

## 实盘 vs 回测口径差异

- TREND_BREAK = 盘前评估（T 收盘指标 → T+1 开盘卖），走 open_sells 带 reason
- TAKE_PROFIT/TRAILING_TP/STOP_LOSS = 盘中价格触发 → broker_conditions 监控表，不进 open_sells
- 买入股数只能预估（T 收盘价口径），实际以次日开盘定
- 涨跌停/停牌在实盘当场可见，次日 sync 自动吸收偏差

## 回归验证

引擎/机制改动后跑 `python scripts/live_e2e_check.py [--bt-db 回测库] [--ledger 账本] [--yaml 策略]`：以回测结果库为 ground truth 遍历 run 自身全部交易日（init → 每日 sync → 每日 signal），操作单必须与回测次日实际成交**逐符号逐 reason 一致**；坏 statement 拒绝、中途建账播种与全程回放等价。注意末日的次日成交不在回测窗口内，所选 run 的最后一日 pending（open_sells/open_buys）需为空，否则脚本会在末日误报差异。
