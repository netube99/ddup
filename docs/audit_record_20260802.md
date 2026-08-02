# ddup 核查记录 2026-08-02（audit agent）

范围：全量 130 项（docs/audit_checklist.md）  代码版本：cf5f709（核查时）→ cf5f709+修复

核查方式：三层核查（静态审查 / 动态实盘 / 数据边界扫描）。静态由 6 个并行审查组逐条核对，
动态由 4 个并行用例组在真实库（`/home/netube/aiwork/tushare_db/data/market.db`，61GB）跑
CLI/回测/探针，主线程跑核心 E2E 冒烟、三轮附录 A 探针与全部修复验证。

---

## 结论汇总

| 模块 | 条目 | PASS | P0 | P1 | P2 | P3 | N/A |
|---|---|---|---|---|---|---|---|
| §1 设计契约 C1-C8 | 8 | 6 | — | 1(C6) | — | 1(D-DIV-03 文档) | — |
| §2 数据层 D-* | 19 | 15 | 1(D-DIV-02) | 2(C6/D-EXT-03) | 1 | 1 | — |
| §3 过滤层 F-* | 8 | 7 | — | 1(F-NEW-01) | — | — | — |
| §4 因子层 FA-* | 16 | 12 | — | — | 3(FA-EX-02/BRD-01/OP-04) | 1 | — |
| §5 策略层 S-* | 18 | 13 | — | 4(S-YML-01/04、S-HOOK-05、S-COND-01) | 1 | — | — |
| §6 引擎撮合 E-* | 23 | 22 | — | 1(E-CFG-01) | — | — | — |
| §7 统计 R-* | 5 | 5 | — | — | — | — | — |
| §8 ML M-* | 9 | 9 | — | — | — | — | — |
| §9 CLI/RS-* | 19 | 15 | — | 2(CLI-XV-01、CLI-BRN-01) | 1 | 2 | — |
| §10 测试基建 T-* | 5 | 4 | — | — | — | — | 1(T-ASSERT-01 部分) |
| **合计** | **130** | **108** | **2** | **11** | **6** | **5** | **1** |

全部 P0/P1 已修复并在真实库重验（除数据层需重新同步的两项，见遗留）。§12 历史缺陷回归
16 项全部复核通过（R-HIS-11 修复不完整，本次补修，见问题 #1）。

---

## 发现并已修复的问题

| # | 级别 | ID | 现象 | 修复 | 验证 |
|---|---|---|---|---|---|
| 1 | P0 | RS-BRN-01 | Brinson 三效应 19/19 天全 NaN，summary 全 0 + unexplained=0.0（"完全解释"假象）：`attribution.py:365-366` 用 `float(h_row.get(ind,0.0))` 取当日未持仓行业，列存在但值 NaN → 污染整日效应；`_aggregate_period:469` 同病 | `_series_get0()` NaN 守卫，两处取数路径统一 | 修复后三效应分解自洽（excess 7.4274% = alloc 0.80+sel 5.02+inter 1.61，unexplained≈0）；新增 2 个回归测试（NaN 行业轮动行 + 聚合层） | 
| 2 | P0 | D-DIV-02 | `get_dividends_on_date` 同 (ex_date,symbol) 多行 last-wins 覆盖：601318.SH 20180607 两行实施 cash_div=1.0+0.2 实测返回 1.0，静默少算 0.2 元/股（17%） | 同键多行：值全等=重复发布取一；异值=叠加求和+WARNING | 601318→1.2 ✓、002352（两行 0.4）→0.4 不翻倍 ✓ |
| 3 | P1 | CLI-XV-01 | cross_validate 资金兜底 `or 40000` 与引擎默认 1_000_000（engine.py:102）不一致：config 未写 initial_capital 时磨损阈值与账户验证失真（实测"设定 40,000"、总收益 2345.73%） | 两处 40000 → 1_000_000 | 修复后"设定 1,000,000"、总收益 -2.17%；并暴露真实报警：rolling_ranker 月磨损 3.09% > 阈值 0.59%（此前被 4 万兜底掩盖） |
| 4 | P1 | F-NEW-01 | 新股名单锚点漂移：`get_recent_listings` 以窗口末日为锚（as_of=end），查询区间 [end-60,end] 漏掉 [start-60,end-60) 的新股——301587.SZ 上市 56 日历日/38 交易日被放行买入 | filters.py cutoff_days 增加窗口长度，覆盖 [start-60,end] | 301587 被滤 ✓、603341（62 交易日）放行 ✓ |
| 5 | P1 | F-ST-01 | 种子日 ST 泄漏：`get_st_map(start_date)` 只覆盖 ≥窗口起，首日前一交易日（种子日）select 决策取空集——全程 ST 的 000017.SZ 在 20210615 窗口首日被买入，零告警 | get_st_map 前伸 10 日历日 | 种子日用例 000017 被滤 ✓；摘帽用例 20210622 恢复可买 ✓ |
| 6 | P1 | E-CFG-01 | 配置校验缺口：initial_capital 负数/NaN 静默；max_positions ≤0 静默（策略永不建仓）；费率四键负值/NaN 静默（stamp_tax_rate=-1 直接虚增卖出净额） | engine.py 补 initial_capital 正有限 + max_positions 正整数校验；costs.py 补费率四键非负有限校验 | 5 种非法配置全部 ValueError，正常构造不受影响 |
| 7 | P1 | S-YML-01 | YAML 顶层未知键完全静默：`filter_rule:`（少 s）typo 静默加载且 FILTER_RULES={}，过滤规则静默失效 | load_strategy 加顶层已知键集校验，未知键 WARNING | typo 键实测 WARNING ✓ |
| 8 | P1 | S-YML-04 | ml_ 列 spec `ascending: "false"`（引号）被 `bool()` 解析为 True 静默反转排序方向（常规因子路径有类型校验） | ml_ 分支与 resolve_spec 对齐：非 bool → ValueError | 实测 ValueError ✓ |
| 9 | P1 | S-COND-01 | trailing 锚点（ConditionBuilder._high）除权日不 rescale：corporate 只缩放当日条件单，次日 calc_conditions 用除权前高点重算触发价 → 开盘即误触发（300501.SZ 10送4.6+派0.6 后 0429 误卖 13.36；正确锚点 12.15 不应卖） | corporate_log 携带 scale，engine 同步 `strategy._cond.rescale()`（新增 ConditionBuilder.rescale） | 300501 除权后无误卖 ✓；entry_price 19.27→12.6413 = ×0.65602 精确 ✓；000001 20260612 除息日不再误卖 ✓ |
| 10 | P1 | CLI-BRN-01 | dump_brinson_data 无 --start/--end 时全表 sw_daily pivot 崩溃（66,110 组 (trade_date,name) 重复 → ValueError），且崩溃先于 --result-db 校验使后者成死代码 | pivot 前 drop_duplicates(subset=[trade_date,name]) | 全表导出 EXIT=0（sw_returns 2081×602）✓ |
| 11 | P1 | C6/D-EXT-03 | stk_holdernumber 以 end_date 为网格键：真实库 (end_date,ts_code) 重复组 9114 → 引用 holder_num 的查询全 fail-fast（功能不可用）；end_date=报告期早于公告 → 前视风险；13% 行非交易日被丢 | 适配器 tables 节 date: end_date → ann_date（公告日对齐，符合 C6 契约；PK(ts_code,ann_date) 天然去重） | 002663.SZ holder_num 查询成功、无重复键 ✓ |
| 12 | P1 | S-HOOK-05 | calc_conditions 返回类型未校验：None/int→TypeError、str/dict→AttributeError 晦涩崩溃、空 dict 静默当作无离场计划 | engine 加 isinstance(list) fail-fast | 144 相关测试全过 |
| 13 | P2 | CLI-SWP-01 | sweep nested_set 不支持列表下标：扫描 factor_specs.0.weight 列表参数时 AttributeError 中断整个扫描 | nested_set 支持整数下标（list 分支） | 实测 factor_specs.0.weight=2.0 ✓、旧 dict 路径兼容 ✓ |
| 14 | P3 | CLI-FEV-01 | factor_eval --model 的 ModelSpec.from_dict ValueError 裸 traceback | try/except 包裹，输出友好错误 | — |
| 15 | P3 | CLI-DMP-01 | dump_fixtures 不读 argv：`--help` 被静默忽略并**真正执行 dump 覆盖 fixtures** | 拒绝任何参数 | --help EXIT=1、fixtures 未被覆盖 ✓ |

文档：D-DIV-03 红利税预扣模型补入 docs/strategy_guide.md §5.4；S-COND-01 除权日 trailing 锚点 rescale 补述。

---

## 遗留问题（记录，未修复）

| 级别 | 说明 |
|---|---|
| ~~P1 数据卫生~~ | **已解决（用户重建 dividend 表）**：原覆盖严重不全（2024 非 BJ"实施"行仅 13 行）→ 重建后 2019-2024 每年 3,000-5,000 条实施行、含退市股与 ann_date NULL 行（182,940 行）。重验：000001.SZ 20240614 除息日现在正确入账 DIV、不再误触发 trailing；2024-06 E2E 出现 CORPORATE 公司行为（此前 0 笔） |
| P2 数据卫生 | *ST 重整股 stk_factor_pro.adj_factor 未反映送转（000908.SZ 恒 2.83，除权日 total_value 跳升 +21,762）——行情表与 dividend 表口径不一致，属数据源问题（dividend 重建后仍存在，上游口径） |
| P2 已知待决 | FA-EX-02 where 两路径 NaN 语义不一致（算子路径静默保留 vs 纯表达式路径 TypeError）；FA-BRD-01 compute_breadth group_mean `.first()` 有损（factor_eval 与引擎结果不一致，"引擎同源"对 group_mean 不成立）；FA-OP-04 group_mean 引擎路径硬编码 industry 分组键 |

---

## 增补：dividend 表重建适配（2026-08-02 同日下午）

用户将 dividend 表从降级快照（PK (ts_code, ann_date)，多阶段互覆、2019-2024 实施记录几乎全丢）
重建为完整多阶段公告表（PK (ts_code, ann_date, div_proc)，182,940 行，ann_date 可 NULL，
含退市股）。引擎适配：

1. **D-DIV-02 聚合语义升级为事件级归并**（`generic_sql.get_dividends_on_date`）：
   - 同 (ex_date, symbol, end_date) → 同事件重复/修订公告，取 ann_date 最新
   - 不同 end_date 但值全等 → 同事件重复记录（end_date 漂移），取一
   - 不同 end_date 且值不同 → 多报告期分红同日实施（叠加事件，求和）+ WARNING
   - 实现用 endds 集合跟踪已归并报告期（避免合并后再遇重复行误叠加）；
     SELECT 带 ORDER BY ann_date DESC（NULL 排最后，老数据优先）
2. **列存在性探测**：end_date/ann_date 是 tushare 表列，通用后端无此二列时
   退化到值级归并（全等取一/异值求和）——PRAGMA table_info 运行时探测
3. **实证**：002352.SZ 20241107 三行实施（中报 0.4 + 特别 1.0 + 重复 0.4）→ 1.4 元/股
   入账（DIV net=4928 = 1.4×4400×0.8 ✓）；601318 20180607 → 1.2；000908 20260311
   两行同值 stk_div=1.0 → 取一（31500→63000 股 ✓）；000001 20240614 → 0.719 入账
   无误卖
4. **残余风险**：div_proc 脏值 `'实施 '`（1 条尾随空格）被 filter 等值匹配排除；
   全库 154 组同 (symbol, ex_date) 多实施行，其中异值叠加组约 10 个（有 WARNING）
5. E2E：rolling_ranker 20240603-28 现为 72 笔（此前 66）、cross_validate 报
   CORPORATE 1 笔（此前 0）；502 测试全绿
| P2 | C6 公告日非交易日记录在网格上不可达（repurchase 101/stk_holdertrade 61 行；stk_holdernumber 改 ann_date 后该问题大幅缓解）——引擎不做公告日→交易日迁移 |
| P2 | D-ST-03 数据观察：北交所 2 只（835305.BJ 等）出现在 stock_st（北交所无 ST 制度，数据怪象）；stock_st ∩ stk_limit 缺失 162 行全为 BJ，回退 30% 恰为正确档位，无错误结果 |
| P3 | FA-YAML-01 load_library docstring 漂移（library.py:54 "where 只允许纯表达式" vs 实际允许算子）；FA-OP-02 log(0)=-inf 非 NaN；E-SET-01 长期停牌估值冻结无告警；E-CORP stk_div 亚股舍入 |
| P3 | CLI-BRN-01 sw_returns 未按 L1 过滤（439 列 vs db 路径 31 列，当前数值无害）；T-FIX-02 fixture 与 adapter 同源仍为人工对齐 |

---

## 附录 A 探针基线（2026-08-02 实测）

- A-01 键类型：stk_factor_pro/stk_limit/stock_st/bak_basic 均 text/text；trade_cal cal_date text；dividend 键 (ts_code,ann_date)；index_weight 键 (index_code,con_code,trade_date)
- A-02 重复键：stk_factor_pro/stk_limit 0 行；dividend 按 (ts_code,ann_date) PK 无重复；**stk_holdernumber (end_date,ts_code) 重复组 9114**（已改 ann_date 键消解）
- A-03 ST：type 枚举仅 'ST'；309,897 行日频快照 20180102→20260731（2026-07-31 当日 208 只）；A-03d 0 行在日历外；摘帽案例 000017.SZ（最后快照 20210621）
- A-04 日历：19901219→20261231，8797 开市日；春节调休正常
- A-05 停牌：vol=0 全表仅 1 行 → **停牌=行缺失**；stk_limit 停牌日有行 → 面板留 NaN 行（002028.SZ 20260708 实证）
- A-06 涨跌停档位（20240102）：ST 1.05 档主流（9 只 1.2 = ST 创业板）；非 ST 主板 1.10；创业板 1.20；**stk_limit.pre_close 全 NULL**（适配器不引用，无影响）
- A-07 分红：div_proc 6 值；拆行真实存在（002352 同值/601318 异值，已修聚合）；10送10 样本 000908.SZ 20260311；除权日停牌样本 002028.SZ 20260708
- A-08 除权：000001.SZ 20240614 ratio 1.0714（2024H1 大量样本）
- A-09 新股：301587.SZ 上市 20240408（20240603 时 36 交易日）；边界票 603341.SH（62 交易日）
- A-10 指数：000300.SH 月频 207 快照（20160129→20260701），最大间隔 36 天 < 45 天
- A-11 公告日：bak_basic 日频快照表（trade_date 键，2063 交易日，0 行在外）；repurchase 101/stk_holdertrade 61 行 ann_date 非交易日
- A-12 事件表起始：limit_list_d 20191128；moneyflow_dc 20230911；top_list/top_inst/kpl_list/moneyflow 20180102
- A-13 北交所裸代码：0 只（全带 .BJ）
- A-14 行业缺失：仅 920238.BJ（index_member_all PK ts_code，l1_name 无 NULL）
- A-15 亏损股 pe_ttm<=0：**0 行**（353,082 样本，140,825 NULL）——2012b39 eps 判定修复方向实证成立

## 动态冒烟（T-E2E-01，主线程）

rolling_ranker 20240603-20240628 真实库：run EXIT=0（69 笔）→ cross_validate 修复后 EXIT=1
（HIGH_COST_RATIO 3.09% > 0.59% 真实报警）→ replay/report/compare EXIT=0。debug 库 2 run 各
69 笔 replay 抽查正常。**注意**：cross_validate 修复后示例策略不再满足"退出码 0"验收——示例
策略月磨损 3.09%（年化 40.78% 拖累）是真实特性，T-E2E-01 验收应改为"无 P0/P1 问题项"或
示例策略降换手。

## 其他

- 全量 pytest：502 passed（含新增 2 个 attribution 回归测试）；check_anticorrupt EXIT=0；
  check_skill_sync EXIT=0（6 skill 一致）。
- checklist file:line 漂移修正约 40 处（各审查组逐条修正，见各 agent 输出 drift 列表）；
  附录 A 探针 SQL 修正 4 处（A-06/A-10b/A-11/A-12 列名与口径）。
- T-ASSERT-01（断言质量抽查 10 个测试）本次未执行——待下轮。
