# ddup 全功能核查列表（Audit Checklist）

> 用途：agent 按本表逐项核查，确保每个功能的**运行行为与设计意图完全一致**。
> 三项核查手段：**静态审查代码 / 动态审查实盘运行 / 数据边界扫描**。
> 每项有稳定 ID（如 `D-ST-01`），核查记录模板见附录 B。

---

## 0. 方法论

### 0.1 根因案例：ST 筛选 bug 为何穿过全部测试

2026-08 发现 `exclude_st` 无法识别 ST 票。事后复盘，测试失效有三个结构性原因：

1. **fixture 与被测代码同源假设**：adapter 用 `type='ST'` 过滤，fixture 生成器（`scripts/dump_fixtures.py`）和 `MockDataBackend`（`tests/conftest.py:143-151`）用**同一个过滤字面量**——测试永远与 adapter 同对同错，无法发现假设本身错误。
2. **fixture 样本空转**：实证 `tests/fixtures/st.parquet`（3 只票）与 `bars.parquet`（沪深300成分+北交所送转股）**交集为 0**——ST 过滤分支在 fixture 下永远无输入，测试"通过"等于没测。
3. **Mock 是第三份手写实现**：`MockDataBackend.get_st_map` 手写镜像而非复用 `generic_sql._impl_get_st_map`（`btcore/generic_sql.py:261-274`）真实 SQL 路径，两处语义漂移无人发现。

**推论**：单元测试只能证明"代码按假设运行"，不能证明"假设符合真实数据语义"。凡依赖真实数据语义的功能（本表以 **🛰** 标记），数据边界扫描层**不可跳过**。

### 0.2 三层核查定义

| 层 | 手段 | 回答的问题 | 通过标准 |
|---|---|---|---|
| **静态审查** | 读代码 + 对照设计文档 | 实现是否与意图一致 | 代码路径、校验、口径与 docs/AGENTS.md 契约逐点吻合 |
| **动态审查** | 真实数据库跑回测/CLI，观测日志、落库、退出码 | 行为是否在实盘中发生 | 有可观测证据（日志行/trade_log/SQL 结果/退出码），不是"没报错" |
| **边界扫描** | 真实数据库探针 SQL + 构造边界样本 | 代码的数据假设是否成立 | 探针结果与代码假设逐条吻合；边界样本行为符合预期 |

**三层全过才算通过。** "读代码看起来对"和"pytest 绿"都不是通过证据。

### 0.3 执行纪律

- **实证优先**：每条结论附证据（日志/SQL 结果/file:line）。禁止凭印象打勾。
- **file:line 会漂移**：引用以核查时的代码为准；发现行号漂移先修正再核查。
- **问题分级**：P0=静默产生错误结果；P1=功能失效但有告警；P2=边界场景错误；P3=体验/文档。
- **一行一证**：动态核查每条命令的输出必须留存（粘贴进核查记录）。
- 发现问题**不得顺手修复后再核查**——先记录原始状态，修复后重跑该条目三层。

### 0.4 全局环境事实（核查基线，2026-08-02 实证）

- 真实数据库：`/home/netube/aiwork/tushare_db/data/market.db`（`adapters/tushare.py:4` `_DEFAULT_DB_PATH`）。
- 运行环境：所有 python 命令用 `uv run python ...`（系统 python 无 pandas）。
- 实盘 ST 数据基线：`stock_st` 全表 309,897 行，`type` 枚举仅 `'ST'` 一个值，日频快照 2018-01-02→2026-07-31；`get_st_map('20240101')` 返回 624 个交易日快照、444 只不同股票、2024-01-02 当日 117 只 ST。**若未来探针结果偏离此基线（如出现 `*ST` 枚举），`type='ST'` 过滤假设立即失效，须重查 `D-ST-01`。**
- fixture 实证基线（`tests/fixtures/`，10 个 parquet）：bars 6,520 行/326 只/20 交易日（2024-06-03→07-01）；st 与 bars 交集=0；无停牌行（vol==0 计数=0）；涨停仅 5 行/跌停 1 行；送转样本全部为北交所 920xxx.BJ；无新股数据；eps 无空值；创业板 2020-08 切换窗口只有 limits 无 bars。

---

## 1. 设计契约核查（全局不变式，任何模块修改后必查）

- [ ] **C1 价格体系** — 意图：撮合/成本/估值用裸价，因子/排名用后复权，绝不混用。
  静态：`_settle` 用 `bar["close"]`（`btcore/engine.py:466-478`，close 取值在 :472-474）；`*_hfq` 由裸价×adj_factor 精确派生不向 backend 请求（`btcore/factors/plan.py:49-56,87-103`）；pre_close 是交易所除权调整口径（`plan.py:34-46`）。
  动态：任选一只除权日持仓，用 `scripts/replay.py` 回放除权当日，确认估值不跳空、因子序列不断崖。
  边界：adj_factor 跳变日（探针 A-08）前后各取一日，hfq 序列连续、裸价序列跳空为正常。

- [ ] **C2 T+1 锁定** — 买入当日 `locked=True` 次日解锁；锁定期间条件单跳过。
  静态：`btcore/match/core.py:21-31`（解锁）、`btcore/engine.py:509-511`、`btcore/match/conditions.py:85-86`（离场跳过 locked）。
  动态：实盘跑含止损策略，cross_validate 确认无同日 BUY+SELL 同标的（SAME_DAY_CONFLICT=0）。
  边界：当日买入当日盘中触及止损价 → 不得成交（INV4 覆盖，但 fixture 无此节奏，需构造样本）。

- [ ] **C3 T 日信号 T+1 执行** — select 当日产出名单次日撮合；条件单 T 声明 T+1 盘中触发。
  静态：`engine.py:382-454` step 顺序（pending 来自前一决策点）；`compute_pending:505+`。
  动态：trade_log 中任意 BUY 的 date 必须晚于产生信号日（replay 对照 pending）。

- [ ] **C4 前视屏蔽三重保护** — ①因子一次性物化为因果列；②provider 查询按模拟日钳制；③T→T+1 时序。
  静态：钳制在 `btcore/provider.py:71-97,108-163`；`get_engine_bars` 刻意不钳制（preload 专用，`provider.py:42-51`）；**`.backend` 直访无防护是约定性的**（`provider.py:1-12`）——策略代码不得直调 backend 传未来日期，静态审查用户策略时逐处确认。
  边界：`get_historical_bars` 的 end 落在非交易日时的 `.loc` 含端点语义（`provider.py:84-96`）；查询 universe 外标的返回空（预载面板只含 load_symbols）。

- [ ] **C5 软回退 vs Fail-Fast** — 可选能力缺失→告警一次继续；明确声明的依赖缺失→加载/preload 报错。
  静态：软回退点：`filters.py:56-91,123-126,163-166` 各 WARNING；fail-fast 点：`plan.py:74-84`（缺必需列 ValueError）、`library.py:183-186`（未知因子）、`generic_sql.py:472-548`（键类型探针）。
  动态：临时注释 adapter 的 `st_symbol` 填表并移除 `tables["stock_st"]` 条目，开启 `exclude_st` 跑回测 → 必须恰好告警一次且回测完成。
  （2026-08-02 实证：仅注释 `st_symbol` 会先触发 `generic_sql.py:680` stray 表 fail-fast——`tables` 节仍声明 stock_st，回测无法开始；须两处同时移除才能走到 `filters.py:53-59` 软回退告警。）

- [ ] **C6 财报数据对齐** — 引擎只消费（交易日，代码）日频网格，财报按公告日对齐由后端物化，引擎不做季度推断。
  静态：`adapters/tushare.py` tables 节 `repurchase/stk_holdertrade/pledge_detail → ann_date`、`stk_holdernumber → end_date`。
  边界：公告日是非交易日的记录（探针 A-11）在网格上的落点行为。

- [ ] **C7 能力开关语义** — 辅助能力以后端是否提供数据为开关，无额外配置。
  静态：`generic_sql.py:78-85` `_EXTRAS` 动态装配；能力空位 `st_symbol/industry_name/listing_date/index_code/index_member/benchmark_close/benchmark_adj_factor`（`generic_sql.py:99-102`）。

- [ ] **C8 禁止重引入清单** — `scripts/check_anticorrupt.py` 13 项机械检查必须绿；另有人工补查项：无 `factors/builtin.py`、无因子类层次、Strategy ABC 无行为开关、无 GuardedProvider、无 ML 外挂模式（策略不得自行加载 ONNX）。
  动态：`python scripts/check_anticorrupt.py` 退出码 0。

---

## 2. 数据接入层（ST bug 的发源地，全部 🛰）

### 2.1 填表机制（generic_sql）

- [ ] **D-SQL-01** 契约 9 列 — `open/high/low/close/vol/adj_factor/pre_close/up_limit/down_limit`（`generic_sql.py:88-91`）。
  静态：实配 `adapters/tushare.py:13-29` 除涨跌停外全在 `stk_factor_pro`，涨跌停在 `stk_limit`。
  边界：`amount` 是扩展字段非契约列，引擎内部不消费——策略若引用须自行声明。

- [ ] **D-SQL-02** 网格语义 — query_bars 以被引用表 (date,symbol) 键外并集拼网格，无行即 NaN（`generic_sql.py:138-179`）。
  边界：停牌日后端**剔行**还是**留 NaN 行**是行存在性约定（`backend.py:59-63`），成交量 cap 对 NaN 不限制（`match/core.py:41-43`）——两种约定下撮合行为不同，必须用真实库确认本库约定（探针 A-05）。

- [ ] **D-SQL-03** 重复键 fail-fast — 同表 (date,symbol) 重复报错并给示例前 3 条。
  动态：探针 A-02 对主表跑重复键检查，确认真实库无重复（否则引擎启动即炸，属数据卫生）。

- [ ] **D-SQL-04** 键类型探针 — 日期列须 YYYYMMDD 文本、代码列须 TEXT（SQLite INTEGER<TEXT 比较恒假会静默查空）（`generic_sql.py:472-548`）。
  边界：新接后端/新表时此项是头号静默杀手，探针 A-01 验证各表键列类型。

- [ ] **D-SQL-05** filter/filter_sql 适用范围 — 仅日历/分红/ST/指数成分四类角色表；对其他表配置 → 初始化期报错（`generic_sql.py:671-696`）。
  静态：实配 tables 节只有 trade_cal/dividend/stock_st 用了 filter（`adapters/tushare.py` tables 节）。

- [ ] **D-SQL-06** 角色表成对约束 — dividends 三空位必须同表；index_code+index_member 成对同表；benchmark_adj_factor 不能脱离 close 单填。

### 2.2 ST 表（本次 bug 现场，重点）🛰

- [ ] **D-ST-01** ST 快照语义链 — 意图：当日有记录=当日 ST，摘帽次日自动恢复可买。
  静态：`generic_sql.py:261-274`（`SELECT symbol,date FROM st WHERE date>=from_date`）；`filters.py:112-113`（`_st_map.get(date_str,set())`）；adapter filter `{"type":"ST"}`。
  动态：跑 `uv run python -c "from adapters.tushare import TushareBackend; b=TushareBackend(); m=b.get_st_map('20240101'); print(len(m), len(m.get('20240102',set())))"` → 基线 624 天快照、2024-01-02 有 117 只。
  边界（三条探针全跑）：A-03a `SELECT DISTINCT type FROM stock_st`（基线仅 `ST`；**出现 `*ST`/`S*ST`/`PT` 即过滤假设失效**）；A-03b 验证表是日频快照而非事件型（同日全表 ST 数量应稳定在百只量级而非个位数）；A-03c 摘帽案例：找曾出现后消失的 symbol，确认其最后快照日次日即不在名单。

- [ ] **D-ST-02** ST 日期列口径 — 快照日期必须是交易日（与日历对齐）。
  边界：若后端表日期是公告日/事件日而非交易日，`st_map.get(date_str)` 查不到即**静默漏判**——探针 A-03d：`SELECT COUNT(*) FROM stock_st s LEFT JOIN trade_cal t ON s.trade_date=t.cal_date WHERE t.cal_date IS NULL`（应=0，注意 trade_cal 的日期列名以库为准）。

- [ ] **D-ST-03** ST 涨跌幅 5% 档位 — 意图：ST 票真实涨跌停 ±5%（主板）。
  静态：`btcore/constants.py:12-19` `PLATE_LIMIT_RULES`（limits.py:56-72 `_get_plate_rate` 消费）**无 ST 档位**——ST 票若 bars 缺 `up_limit/down_limit` 列会被按 10% 推导。实配从 `stk_limit` 表取精确值，属正常；**但任何缺列回退路径对 ST 票都是错的**。
  边界：探针 A-06 抽查 ST 票的 `stk_limit` 覆盖率（有无缺失行）；缺失即 P1。

### 2.3 分红表 🛰

- [ ] **D-DIV-01** 过滤口径 — `div_proc='实施' AND ex_date IS NOT NULL`（`adapters/tushare.py` tables 节）。
  动态：探针 A-07a 确认真实库存在 div_proc 其他值（预案/实施进度），验证过滤必要性。

- [ ] **D-DIV-02** 每股送转/现金口径 — 引擎假设 `stk_div`=每股送转比例、`cash_div`=每股现金（税前）。
  边界："10送3转2派1" 拆多行还是合一行（探针 A-07b 按 (symbol,ex_date) 分组查重复）；拆多行时 `get_dividends_on_date` 的聚合行为（`generic_sql.py:193-213`）必须实证。
  （2026-08-02 更新：dividend 表重建为多阶段公告表后，聚合升级为**事件级归并**——同
  (ex_date,symbol,end_date) 取 ann_date 最新（重复/修订）；不同 end_date 值全等取一
  （end_date 漂移的重复记录）；不同 end_date 值不同按叠加求和+WARNING（多报告期同日
  实施，如 002352.SZ 20241107 中报 0.4+特别 1.0=1.4）。通用后端无 end_date/ann_date
  列时退化到值级归并（PRAGMA 探测）。实证：601318=1.2、002352=1.4、000908 取一不×2。）

- [ ] **D-DIV-03** 红利税口径 — 除息日按持仓期预扣（≤30d 20% / ≤365d 10% / 否则 0）（`btcore/corporate.py:45-68`）。
  静态：真实规则是**卖出时按最终持有期补扣/退还**，引擎预扣方向保守但现金序列与实盘有差——设计取舍，核查时确认文档（`docs/strategy_guide.md`）有明示，不算 bug 但必须知情。

- [ ] **D-DIV-04** 送转取整 — `max(1,int(shares×(1+stk_div)))` 截断零碎股，entry_price 按满比例缩放（`corporate.py:33-42`）。
  边界：大比例送转（10送10+）+小仓位时账面微量误差；fixture 送转样本全部北交所，主板/创业板送转行为需真实库案例（探针 A-07c 找 10 送转 ≥10 的样本）。

- [ ] **D-DIV-05** 除息日停牌 — 无 bar 无法做 pre_close 缩放，entry_price 保持除权前口径（已知取舍，`docs/strategy_guide.md` 有述）。
  边界：探针 A-07d 找除权日无 bar 的真实样本，构造用例确认不崩、口径差在可接受范围。

### 2.4 日历 / 行业 / 上市日期 / 指数成分 / 基准 🛰

- [ ] **D-CAL-01** 交易日历 — `trade_cal filter{exchange:SSE,is_open:1}`。
  边界：春节/国庆调休；日历覆盖区间必须 ⊇ 回测区间+warmup 前伸（365 天），探针 A-04 查 MIN/MAX。

- [ ] **D-IND-01** 行业映射 — `index_member_all.l1_name`（申万一级）是**当前时点**映射，非历史 PIT。
  边界：历史换行业的股票，因子分组（`group_mean`/`neutralize`）与 Brinson 归因用当前行业——设计取舍，归因结果解读时必须知情。

- [ ] **D-NEW-01** 上市日期 — `stock_basic.list_date`；`get_recent_listings(60, as_of=end or start)`（`generic_sql.py:289-302`）。
  静态：as_of 用回测 end 防前视（名单在加载期固定）。
  边界：fixture 无新股数据（恒空桩），此规则只有真实库能验证（探针 A-09）。

- [ ] **D-IDX-01** 指数成分快照 — `_index_members_at` 取"最近一期 ≤ 当日"快照，加载前伸 45 天（`filters.py:11,72-98`）。
  边界：真实 `index_weight` 快照频率（探针 A-10a：月频约 2 次/月）；调样生效日边界；首期快照之前的日期用首期（fail-open）；快照缺口超 45 天时回测首日取不到成分（告警+不生效）。

- [ ] **D-BMK-01** 基准 — `idx_factor_pro.close` 指数点位无复权概念，不填 `benchmark_adj_factor`；hfq_close=close。
  边界：`benchmark` 配置空串=无基准；`derive_idx_ret` 用基准 hfq_close pct_change（`plan.py:106-119`），指数本身除权/停牌日失真。

### 2.5 扩展字段 extra_fields（约 110 个，`adapters/tushare.py:40-133`）🛰

- [ ] **D-EXT-01** 事件型稀疏列 — `limit_list_d`（2019-11 起仅上榜日有值）、`top_list`（2018 起）、`moneyflow_dc`（2023-09 起）、`top_inst`/`kpl_list`/`pledge_detail`（适配器 2026-07-30 新增列；数据起始 20180102）。
  边界：未上榜日=NaN 是**语义**不是缺失——因子用 `sum(x>0,5)` 计数时 NaN 与 0 不等价（ops `sum` 遇 NaN 整窗 NaN）；各表起始日探针 A-12（起始日之前的回测区间该列全 NaN，因子静默全 NaN → `validate_materialization` NaN>5% 只 warning，`plan.py:317-355`）。

- [ ] **D-EXT-02** 单位契约 — `vol` 手 vs `amount` 元；`seal_strength` 元/万元换算（`factors/library.yaml.template:40-43` 注释，`docs/factor_library.md:353-355`）。
  静态：因子表达式引用扩展列时逐个核对量纲注释；量纲错不报错，全链路静默错。

- [ ] **D-EXT-03** 季度/低频表 — `stk_holdernumber(end_date)`、`bak_basic`。
  边界：低频表 join 到日频网格的填充语义（LEFT JOIN 后非报告日 NaN）；因子消费前须有明确的填充/掩码设计。

---

## 3. 过滤层 StockFilter（逐规则，`btcore/filters.py`）

统一前置：规则默认关闭（未声明=不生效不告警不 preload，`filters.py:14-24,115-120`）；后端缺方法→告警一次+不生效（软回退）。
**动态验证统一方法**：构造真实库回测，开启单规则，`replay.py` 抽查被过滤/被放行标的是否符合预期；`cross_validate.py` 退出码 0。

- [ ] **F-ST-01** exclude_st — 见 `D-ST-01` 全链核查；另验证**摘帽次日恢复可买**：构造含摘帽窗口的回测，确认摘帽后该票重新出现在可选池。
  （2026-08-02 实证备注：摘帽次日恢复 ✓（000017.SZ 窗口 20210622-20210705 成交）；但**种子日 ST 过滤泄漏**：`get_st_map` 只覆盖窗口起（`filters.py:53-54` 传 start_date），`filters.py:131` 对缺失日期取空集——首日前一交易日 select 决策绕过 ST 过滤，次日（若仍 ST）照买，实测窗口 20210615-20210618 买入了全程 ST 的 000017.SZ → P1。）
- [ ] **F-NEW-01** exclude_new_stock — 上市 ≤60 日剔除。🛰 fixture 恒空桩（`conftest.py:156-159`），只有真实库可验证：探针 A-09 找上市 59/61 日边界的票，回测中验证 59 日被剔除、61 日放行。
  （2026-08-02 实证备注：实现是日历日口径 + 以**窗口末日**为锚——`filters.py:61-64` 传 `as_of=end_date`，`generic_sql.py:289-303` 用 `timedelta(days=60)`；301587.SZ（上市 20240408）在窗口 20240603-20240610 下 cutoff=20240411 → 放行，而 20240603 当日该股仅 38 交易日/56 日历日 <60 应剔除 → 边界不符 P1。）
- [ ] **F-BRD-01** exclude_boards — `_get_board` 代码前缀映射（`filters.py:180-190`）：`.BJ`→BJ、688→688、300/301 各自、其余 MAIN。
  边界：**无后缀的北交所代码（8xxxxx/4xxxxx）归 MAIN**——探针 A-13 确认本库北交所代码是否都带 `.BJ` 后缀；若有裸代码则过滤失效。
- [ ] **F-IND-01** exclude_industries — 懒加载行业映射（`filters.py:116-126`）；`industry=None`（未分类）**软通过**（`filters.py:142-145`）。
  边界：探针 A-14 统计行业映射缺失的 symbol 数；缺失票在规则开启时静默放行。
- [ ] **F-PRC-01** min_price — 按裸价 close 过滤，close 缺失默认 0.0 被过滤（`filters.py:151-153`）。
- [ ] **F-LOS-01** exclude_loss — eps<0 判定；无 eps 回退 pe_ttm<=0 并告警一次（`filters.py:155-173`）。🛰
  边界：tushare 亏损股 pe_ttm 为 NULL 或**正数**（可达 4e4），pe_ttm<=0 结构性失效（2026-08-02 修复的 bug）；探针 A-15 验证真实分布：亏损股（eps<0）中 pe_ttm<=0 的占比应极低。列裁剪下未声明规则不 preload eps（`filters.py:14-24`）。
- [ ] **F-IDX-01** index_universe — 只管入场白名单；快照机制见 `D-IDX-01`。
  边界：无成分→告警+规则不生效（fail-open 全量，`filters.py:82-86`）——配置错指数代码时静默全市场选股，动态验证必须确认告警被注意。
- [ ] **F-FUN-01** factor_universe — 因子计算域可宽于交易域，引擎裁回交易域（`engine.py:246-254`；裁后空→ValueError）。
  边界：缺 `get_index_members` 时告警回退=交易域（`strategy_loader.py:235-265`）。

---

## 4. 因子层

### 4.1 算子（21 个，`btcore/factors/ops.py` `_OPS:205-241`，固定表非注册表）

- [ ] **FA-OP-01** 时序算子窗口语义 — delay/delta/roc=n，ma/std/sum/max/min/corr/beta/resid_std=n−1，ema=3n−1（`ops.py:325-355` `infer_window`）。
  边界：**rolling 按组内行数非自然日**——停牌缺行不占窗口但 `sum` 遇 NaN 整窗污染；fixture 数据连续掩盖此行为，需构造含缺行面板验证。
- [ ] **FA-OP-02** NaN 语义逐算子 — roc 基准=0→NaN；corr/beta 分母 0→NaN；zscore 当日 std=0→NaN；log x≤0→NaN（x=0 实际为 -inf，ops.py `_xs_log`）；**BoolOp `(v!=0)` 折叠使 NaN→True**（`ops.py:408-413`）——and/or 链中 NaN 按真处理，是静默错误高发点。
- [ ] **FA-OP-03** 截面算子口径 — rank(pct=True) 并列取平均秩（0/1 布尔因子大量并列时分布反直觉）；winsorize 分位∈(0,0.5) 字面量校验（`ops.py:310-311`）；neutralize 当日有效样本≤变量数→全日 NaN（`ops.py:159-160`，新股/小截面触发）。
- [ ] **FA-OP-04** 坍缩算子 — mean 全市场日频均值广播；group_mean 分组键**引擎侧硬编码 industry**（`plan.py:395-421`）——非 industry 分组键在引擎路径直接 NaN。
- [ ] **FA-OP-05** ema 无限记忆 — warmup=窗口时前段有偏（3n−1 近似），首月 ema 类因子值与长 warmup 口径有差。
- [ ] **FA-OP-06** beta/corr/resid_std 闭式矩为**总体口径**（ddof 在比值中抵消），与 sklearn 样本口径数值级差——第三方 golden 对账会误报，属已知口径。

### 4.2 表达式与物化

- [ ] **FA-EX-01** 非法表达式加载期报错 — `expr.py:22-43`（Call/Attribute→ValueError；单行试算失败→ValueError；lru_cache 不缓存错误）。
  边界：单行试算 `{n:[1.0]}` 可能掩盖伪列运行时缺列——`library.py:287-299` `_detect_missing_columns` 补救，两防线都要在。
- [ ] **FA-EX-02** where 子句两路径 NaN 语义不一致（**已知待决**）— 算子路径 `astype(bool)` NaN→True 不掩码（`plan.py:384-392`）；纯表达式路径 `df.eval(where)` 的 NaN 被掩码为 False（`expr.py:71-76`，掩码行 :76；NaN 非 bool where 实为 TypeError 崩溃，见 2026-08-02 静态实证）。
  静态：确认差异存在；两路径各造一个 where 含 NaN 的因子验证行为。P2。
- [ ] **FA-PL-01** 两路供给 — 主面板=候选池×长窗口；广度=全市场×短窗口窄列（`plan.py:161-257`；main_days=max(365,breadth_days)）。
  边界：两面板同节点各算一次，主=候选池口径、广度=全市场口径——坍缩因子值在引擎与 factor_eval 下应一致（`scripts/factor_eval.py:186-208` 已同源，回归项 R-HIS）。
- [ ] **FA-PL-02** 缺列 fail-fast — `validate_required_columns`（`plan.py:74-84`）契约强制无兜底。
- [ ] **FA-PL-03** warmup 推导 — 交易日→日历天 ×1.5+10，地板 365 天（`plan.py:47,157-158`）。
  边界：回测首日因子值必须与更长 warmup 口径一致（warmup 不足导致的首月偏差，动态对比法：同一因子两个 warmup 长度跑 factor_eval 比对首日值）。
- [ ] **FA-CSE-01** CSE — 完全重复按 ast.dump 文本同构去重（`cse.py:39-49`）；含坍缩子树不提取（:72-74）；临时列物化后删。
  边界：`mom60`/`mom_60d` 别名去重后只剩一份列——测试/策略若断言两列独立存在会失败。
- [ ] **FA-BRD-01** compute_breadth — 分块 60 天全市场流式（`library.py:302-405`）；**group_mean 坍缩 `groupby(trade_date).first()` 只取某一行业组值，日频标量有损（已知待决 P2）**；chunk 边界依赖日期字符串字典序，缺口日历下需真实验证。
- [ ] **FA-YAML-01** library.yaml 加载防线 — 重复键 fail-fast 带行号（`library.py:27-38`）；保留字 20 个（:41-47）；环检测 DFS（:209-229）；缺 expr→ValueError。
  边界：`load_library:73-77` 允许 where 含算子调用，与 library.py docstring"where 只允许纯表达式"矛盾（**文档漂移 P3**），以代码为准并修正 docstring。

### 4.3 因子口径动态验证（引擎同源三件套）

- [ ] **FA-DYN-01** factor_eval 与引擎同源 — warmup 前伸、坍缩全市场、PIT 成分（2026-08 已修，75274e6）。
  动态：同一因子同一区间，`factor_eval.py` 的 IC 序列与引擎内物化列抽样比对（replay 快照含因子列）。
- [ ] **FA-DYN-02** 事件计数类因子 — sealed_days5/pct_sealed 等依赖 `limit_list_d` NaN 语义（未上榜=NaN≠0）。🛰 探针 A-12 起始日 + 边界扫描 NaN 计数行为。

---

## 5. 策略层

### 5.1 YAML 加载（`btcore/strategy_loader.py`）——校验不对称是静默失效高发区

- [ ] **S-YML-01** 未知键三级处理不对称 — `filter_rules` 未知键→**WARNING 忽略**（:324-327）；`conditions` 未知键→ValueError（:333-337）；**YAML 顶层未知键→完全静默忽略**。
  静态：确认三级行为；动态：已实测（2026-08-02）——顶层 typo 键（`filter_rule:` 少 s）**完全静默**，即使 typo 键是唯一 filter 配置也照常加载（FILTER_RULES={}）。
- [ ] **S-YML-02** select 协议键校验 — `_SELECT_KEYS` 6 键（`engine.py:19-23`）；未知键 WARNING 忽略（:549-553）。
- [ ] **S-YML-03** conditions 比例 ∈(0,1) 校验（:353-356）；model_exit 模型已声明（build_strategy:90-98）+threshold∈(0,1)（:347-351）。
- [ ] **S-YML-04** bool 陷阱 — **仅 ml_ 列分支** `bool(raw.get("ascending"))` 对引号写法 `"false"` 得 True（:311）；常规因子走 `resolve_spec` 有类型校验 fail-fast（factors/library.py:148-150）。
  边界：ml_ 列 spec 中 `ascending: "false"`（带引号）静默反转排序方向（常规因子则直接 ValueError）。已实测（2026-08-02）：常规因子报错、ml_ 列静默 True。
- [ ] **S-YML-05** factor_specs 引用校验 — 未声明 ml_ 列→ValueError（:300-315）；引用 holding scope 列→ValueError（:111-115）；materialize_only 与 scoring 交叉 WARNING（:358-390）。
- [ ] **S-YML-06** 程序化 vs 文件路径不对称 — `factor_library` 相对路径只在 YAML 加载解析，程序化 `build_strategy` 以 CWD 解析。

### 5.2 Strategy 钩子时序（`btcore/strategy.py` + `btcore/engine.py`）

- [ ] **S-HOOK-01** 调用顺序 — preload（get_universe/get_factor_universe，as_of 锚定首日前一交易日 `engine.py:208-210`）→ attach_bars → on_start → 每日 on_fills→on_tick→select→calc_conditions。
- [ ] **S-HOOK-02** on_start 防线 — 覆盖 on_start 不调 super() 且配了 FILTER_RULES → filter_bars 时 RuntimeError（`strategy.py:139-153`）。
  动态：示例策略全开 filter_rules 跑通，确认 StockFilter 已构建。
- [ ] **S-HOOK-03** holding_days 口径 — calc_conditions 收到的 holding_days **已 +1**（`engine.py:509-510,671`）——按"入场后第 N 天"直觉写策略会差 1 天。
- [ ] **S-HOOK-04** on_tick 返回 — 仅允许 buy_conditions 键，其余 ValueError（`engine.py:557-571`）；默认实现 prune trailing 锚点（`strategy.py:118-127`）。
  边界：同一 Engine 实例重跑，ConditionBuilder._high 锚点残留（run 重置账户不重置 ConditionBuilder）——程序化多次 run 需新建策略实例。
- [ ] **S-HOOK-05** calc_conditions 返回空 = 无离场计划 — 技术上合法，审查策略时标记为不完整（AGENTS.md 五要素第 3 条）；返回类型未校验（非 list → AttributeError，**软缺口**）。

### 5.3 select 协议与 ConditionBuilder

- [ ] **S-SEL-01** 名单防线 — buy/sell 非空串、无重复、同日不冲突（`engine.py:26-40,573-585`）；重复 symbol 曾致账户腐化（历史 P0，已修，回归项 R-HIS）。
- [ ] **S-SEL-02** target_value — 与 buy/sell/buy_conditions 互斥；值 ≥0 有限数值校验；target=0 清仓含零碎股；未列出持仓不动（`engine.py:586-598`；`match/manual.py:173-304`）。
- [ ] **S-SEL-03** buy_weights — 键集恰等于 buy、∈(0,1]、和 ≤1（`engine.py:614-631`）。
- [ ] **S-SEL-04** buy_conditions — {symbol,type,price}+value/shares 恰一；与 buy/sell 名单不重叠（`engine.py:633-664`）。
- [ ] **S-COND-01** 条件单价格口径 — stop_loss=entry×(1−pct)；take_profit=entry×(1+pct)；**trailing 锚点=持仓期最高收盘价**（触发在盘中 low，`strategy_tools.py:93-128`）。
  边界：除权日 close 跳变使 trailing 锚点台阶式变化（公司行为 rescale 应抵消，`corporate.py:71-78`，构造除权+trailing 用例验证）。
- [ ] **S-COND-02** 已持仓再买 — buy 名单含已持仓 symbol 静默跳过（`match/manual.py:98,105-106`）；条件买单已持仓静默跳过（`conditions.py:201-202`）——策略必须显式决定加仓策略，审查时确认这是设计意图而非漏单。
- [ ] **S-COND-03** 同日止损+接回 — exit_conditions 卖出 X 后 entry_conditions 同日可再买回 X（互斥校验只覆盖 sell 名单）——确认策略意图。

---

## 6. 引擎与撮合

### 6.1 主循环与配置

- [ ] **E-LOOP-01** step 日序 — 除权除息→TARGET 调仓或（手动卖→手动买）→离场条件单→入场条件单→结算落库→compute_pending（`engine.py:382-454`）；异常回滚账户状态（`_save_state/_restore_state:759-782`）。
- [ ] **E-LOOP-02** 无行情日跳过 + WARNING（`engine.py:280-284`）；首日播种用首日前一交易日截面（:276-278）。
- [ ] **E-CFG-01** 配置校验覆盖表 — 已校验：slippage_ticks/condition_slippage_ticks（:110-126）、order_volume_ratio（:141-149）、execution_price（:151-155）。**未校验缺口**：`initial_capital` 无正数/有限校验（:100-103）、`max_positions` 无 ≥0 校验（:105-108，≤0 时 manual_buy 静默返回[]）、**费率四键无校验**（`costs.py:10-13`，负费率/NaN 静默虚增收益）。
  静态：确认缺口仍存在或已修；动态：构造负费率 config 观察是否静默（若静默=P1 缺口）。
- [ ] **E-CFG-02** benchmark 推导 — 未配置时 index_universe 单指数，否则 000300.SH；空串=无基准（`engine.py:128-139`）。

### 6.2 撮合（`btcore/match/`）

- [ ] **E-MCH-01** 价格防线 — None/NaN/非正一律拒单（`core.py:4-6`）；各拒因独立 WARNING（quiet_skips 可静默）。
- [ ] **E-MCH-02** 涨跌停 — 真实列优先，缺列按板块规则推导（`limits.py:8-54`）：主板 10%、300/301 自 2020-08-24 20%、688 20%、BJ 30%；**ST 5% 无档位（见 D-ST-03）**；缺 pre_close/未知板块→(None,None) 不拦截。
  动态：构造涨停日买单确认被拒且 WARNING（INV8 覆盖基础态，但创业板切换窗口 fixture 只有 limits 无 bars，需真实库样本）。
- [ ] **E-MCH-03** 成交量 cap — 单笔 ≤ int(vol手×ratio)×100；vol NaN 不限制（`core.py:34-46`）；截断后 <100 股跳过。🛰 行为依赖后端停牌日行存在性约定（见 D-SQL-02）。
- [ ] **E-MCH-04** 现金防线 — 现金不足跳过不缩股（`manual.py:145-151`）；est 含滑点+费。
- [ ] **E-MCH-05** 整手取整 — 买侧 100 股整数倍；卖出可零碎（INV2）。
- [ ] **E-MCH-06** 停牌 — bar 缺失即停牌/缺数据，跳过+WARNING，卖出不顺延（pending 次日重算）。🛰 fixture 无 vol=0 样本，停牌路径需真实库验证（探针 A-05 找真实停牌日样本构造用例）。

### 6.3 条件单（`btcore/match/conditions.py`）

- [ ] **E-COND-01** 内置三单触发规则 — STOP_LOSS：open≤price→open，否则 low≤price→price（:143-159）；TAKE_PROFIT 对称；TRAILING_TP 复用 STOP_LOSS 规则。INV7 成交价 ∈[low,high]。
- [ ] **E-COND-02** 逐持仓按序评估 — 每持仓每日至多成交一条，首条触发即 break；部分成交后次日再评（:79-141）。
- [ ] **E-COND-03** required_keys 决策时点 fail-fast — T 日发现缺键即 ValueError，不拖到次日撮合 KeyError（:46-76）。
- [ ] **E-COND-04** 自定义 handler 注册 — 进程级全局 `_DISPATCH/_REQUIRED_KEYS`（:25-38）；cross_validate 对注册表外 trigger 仅 INFO（`scripts/cross_validate.py:99-105`，2026-08 已修）。
- [ ] **E-COND-05** 条件买单时效 — T 声明 T+1 单日有效，未触发自动失效；吃当日卖出释放的现金（`engine.py:393-436` 顺序保证）。

### 6.4 成本 / 滑点 / 公司行为

- [ ] **E-COST-01** 成本 — 佣金 max(额×率，最低 5)+卖印花税 0.0005+过户费 0.00001（`costs.py:4-25`；`constants.py:1-4`）。
  动态：trade_log 抽样手算一笔佣金（小额单触发 MIN_COMMISSION）；cross_validate 磨损阈值分档（`cross_validate.py:141-155`）。
- [ ] **E-SLIP-01** 滑点 — round(price±ticks×0.01,2)，买入不利方向为正（`slippage.py:4-5`）；Trade.price 含滑点、slippage_amount 记正成本。
- [ ] **E-CORP-01~05** 公司行为 — 见 D-DIV-01~05（数据层核查同源）；另核 INV6 守恒：送股后 shares×price 守恒、现金分红 cost 扣净额。

### 6.5 估值 / 落库 / 可观测

- [ ] **E-SET-01** _settle — 裸价 close 估值；close 非法沿用 last_price+告警（`engine.py:456-483`）。
  边界：长期停牌持仓估值冻结在 last_price——确认告警可见。
- [ ] **E-DB-01** 落库契约 — runs.status running→completed/failed；trade_log 含 DIV/STK_DIV 行（送转 shares=送转后总股数，trigger=CORPORATE，`engine.py:495-512`）——stats 往返盈亏/Brinson 重建/ML 回合配对全部依赖此约定（2026-08 修复，8593bfd）。
- [ ] **E-DBG-01** debug 快照 — Engine(debug=True) 每日落 {account,pending{buy,sell,buy_conditions},holdings_detail,bars_subset}（`engine.py:784-826`）；**不含 target_value、不含 sell 名单 bars**——replay 时知情。
- [ ] **E-PROV-01** backend 直访无防护 — 策略直调 `provider.backend.get_*` 传未来日期可拿未来数据；审查用户策略时 grep `.backend` 逐处确认。

---

## 7. 统计与结果库

- [ ] **R-STATS-01** 往返盈亏口径 — 统一事件流 BUY/SELL/DIV/STK_DIV（`btcore/stats.py:216-311`，事件流 232 起）；STK_DIV 缩放 lot 队列（股数×比例、总成本不变）；**pnl 含费用口径（CONS-01：买入成本与卖出净额均按比例分摊 net_amount，与 symbol_contribution/ML 标签一致）**；超卖静默丢弃改告警（CONS-04）。
  动态：构造送转案例，stats 的 round_trip pnl 与手算对账（2026-08 修复实证：10送10 后卖 200 股曾 pnl 错算，8593bfd；CONS-01 后含费用口径 1013.39-1007.1=+6.29）。
- [ ] **R-STATS-02** stats_json 键名契约 — annualized_return/sharpe/calmar + 嵌套 round_trip/trading_friction；report/cross_validate 消费同一键名（2026-08 已对齐，c18c269）。
- [ ] **R-DB-01** schema 迁移 — 旧库无 stats_json 列 ALTER 补列，历史 run 读侧现场重算（缺 benchmark/期末浮盈口径略少，`research/report.py:78-116`）。
- [ ] **R-DB-02** 多 run 累积 — 同库复用保留历史 run；holdings 表每次 run 清空（瞬态快照）；sweep 写标准 runs 表（2026-08 已修，5a1caeb）。
- [ ] **R-INV-01** INV1-8 与实盘对账 — 不变量测试断言的账户恒等式/手数/现金非负/T+1/买卖互斥/公司行为/条件单价范围/涨跌停，在真实库回测后用 cross_validate + SQL 抽查复核（fixture 通过≠实盘通过）。

---

## 8. ML 子系统

- [ ] **M-META-01** meta v3 契约 — version≠3 一律拒绝（`btcore/ml/spec.py:33,127-131`）；列序=factors+raw+state_features；scaler 维度加载期 fail-fast（:185-200）+ 运行时 RuntimeError 兜底（`runtime.py:46-57`）；artifact_sha256 随 config_json 落盘（:214-222）。
- [ ] **M-SCOPE-01** 双 scope — state_features 非空→holding 否则 panel（spec.py:75-77）；panel 物化 ml_<name> 列（`runtime.py:94-123`；`engine.py:567`）；holding 决策时点批量注入 bar dict（`runtime.py:160-195`；`engine.py:814-838`）。
  边界：混用 scope 的引用防线（loader ValueError）逐条触发验证。
- [ ] **M-STATE-01** state_features 口径 — hold_days 交易日口径；ret_from_entry **裸价/裸价**（v2→v3 修复点，`runtime.py:139-157`）。
  边界：除权日持仓的 ret_from_entry 连续（v2 曾跳变 −15pp）；fixture 无除权-持仓联合场景，真实库构造用例。
- [ ] **M-LABEL-01** 标签 — panel：xs_forward_return=hfq 前向收益截面 pct rank（`labels.py:29-41`）；holding：TREND_BREAK 且净亏损=正类，距卖出 [1,lookahead] 交易日（:124-189）。🛰 holding 标签依赖真实 trade_log 的条件单 trigger 分布。
- [ ] **M-PAIRS-01** 回合语义 — 持仓 0→0 一回合，买入不限 trigger，STK_DIV 同步 buy_shares，残缺回合跳过+告警（`labels.py:44-122`）。
- [ ] **M-TRAIN-01** 训练同源 — dataset.build_panel 逐行复刻引擎物化链（`dataset.py:23-104`）；PIT 训练域（:107-122）；time_split 80/20+embargo（`trainer.py:32-49`）；样本下限 panel≥500/holding≥100 且正样本≥20。
- [ ] **M-NAN-01** 缺失护栏 — NaN/±inf 在 scaler 后填 0（=训练段均值中性）；缺失过半无分数（`runtime.py:59-78` 填 0；`runtime.py:110-113` panel / `185-190` holding 缺失过半护栏）——依赖"训练段均值=中性"假设，因子含义偏移时此假设需重估。
- [ ] **M-EXIT-01** model_exit — holding 分数≥threshold 生成 ML_EXIT 条件单，T+1 开盘成交。
  边界：**post_transform=xs_rank 且单持仓时分数恒 1.0 每日必触发**（loader 有 WARNING，`strategy_loader.py:99-106`）；动态构造单持仓回测确认告警出现。
- [ ] **M-OBS-01** 可观测 — ml_predictions 落盘（ml_log=full 落全截面，缺省只落决策标的，`engine.py:720-757`）；样本内重叠告警（`engine.py:677-692`）。
  边界：ml_log=full 分支无测试（已知盲区）；export_model/_verify sklearn↔ONNX 一致性无直接测试（test_ml 绕过）。

---

## 9. CLI 与研究工具（13 CLI + 4 research 模块）

动态验证统一入口：每个 CLI 用真实库跑一遍，确认退出码、落盘行为、输出内容与下表一致。

- [ ] **CLI-RUN-01** run.py — `--out` 缺省**内存库不落盘**；`--report` 缺省 auto 产 HTML 到 `<策略目录>/reports/`；`--capital` 覆盖 config；finally 必 `backend.close()`（`scripts/run.py:38-63`）。
- [ ] **CLI-RPT-01** report.py — `--run-id` 缺省最新；老 run stats_json=NULL 现场重算口径略少（`research/report.py:78-116`）。
- [ ] **CLI-CMP-01** compare.py — <2 run 退 1；对比表 11 项（`research/report.py:548-560`）。
- [ ] **CLI-FEV-01** factor_eval.py — 纯终端不落盘；fail-fast 集：未知因子/scope≠panel/--decay 与非默认 --forward 互斥/嵌套坍缩引用（`scripts/factor_eval.py:69-405`）；--model 评 ML 分数（scope 须 panel）。
- [ ] **CLI-XV-01** cross_validate.py — **退出码=问题数**；10 检查项（trigger 分布/买卖比/同日冲突/公司行为/小单/卖出分类/频率/现金非负/持仓上限/权益）；磨损阈值=最低佣金×2+印花+分档 variable（`scripts/cross_validate.py:73-231`）。
- [ ] **CLI-SWP-01** sweep.py — `--out` 缺省 sweep_result.db 落盘；单组失败 continue；每组=标准 run+sweep_results 表（`scripts/sweep.py:23-124`）。
- [ ] **CLI-RPL-01** replay.py — 依赖 debug=True 快照；--run-id 缺省最新（旧库无 runs 表则报错『结果库中无 run 记录』退出，无 run 1 回退——`scripts/replay.py:33-39`，代码注释『回退 run 1』与实现不符）。
- [ ] **CLI-MLT-01** ml_train.py — holding 缺 --db 报错；meta version≠3 拒绝；YAML 写 post_transform 无效（以 meta 为准）（`scripts/ml_train.py:67-160`）。
- [ ] **CLI-DMP-01** dump_fixtures.py — 窗口固定 20240601-0701（limits 额外 20200820-25）；ST 宽窗 dump 但 bars 主窗窄窗——**st 与 bars 交集为 0 的结构性空转即源于此**，改造 fixture 前先读 MlTestScout 实证统计（本文 §0.4）。
- [ ] **CLI-BRN-01** dump_brinson_data.py — `--index` 过滤防多指数混入（2026-08 修复；SQL `WHERE iw.index_code=?` 硬过滤，缺省值 000300.SH 亦安全）；`--result-db` 须配 start/end 才导 bars。
- [ ] **CLI-LINT-01** check_anticorrupt.py — 13 项检查（调用点 `scripts/check_anticorrupt.py:390-402`，docstring 13 条 1-19 行，与实际一致）；AGENTS.md 声称的 5 条架构规则曾经无检查（已补），核对 docstring 与实际检查数一致。
- [ ] **CLI-SYNC-01** check_skill_sync.py — 7 项对账（CLI flag/算子/select 键/filter 键/条件单键/meta v3/config 默认值）；接口变更后必跑。
- [ ] **RS-FEV-01** research.factor_eval — IC<3 样本→NaN；分层 q=1 最低档；衰减 fwd_ret 用 close_hfq；corr<3 行跳过（`research/factor_eval.py:10-166`）。
- [ ] **RS-CMP-01** research.composite — 滚动 IC/ICIR 权重只用 ≤t-1 日 IC（shift(1) 前视保护，`research/composite.py:29-77`）；全部无 IC 行保持 NaN 非 0。
- [ ] **RS-BRN-01** research.attribution — 三效应公式、持仓逐日结转、STK_DIV 重建、基准快照 ffill、数据不足返回 {"error":...} 不抛异常；`brinson_attribute_from_files` **run_id 缺省 1（非最新）**。
- [ ] **RS-RPT-01** research.report — 股票名加载失败静默降级裸代码（:25-59）；基准对比 Alpha/Beta/IR 依赖 stats_json.benchmark_nav（不落库时报告缺基准叠加）。

---

## 10. 测试基础设施有效性（meta 层：防花架子）

本节核查**测试体系本身**的检出能力——ST bug 证明这套体系会系统性漏掉真实数据语义类缺陷。

- [ ] **T-FIX-01** fixture 代表性审计 — 每季度重跑 §0.4 基线统计（脚本化读 parquet）：bars 行数/symbol 数/日期范围；边界样本六查：ST 票、停牌、涨跌停、除权、板块覆盖、空值分布。**当前基线六项中四项缺失**（ST/停牌/新股/创业板切换窗口 bars）。
- [ ] **T-FIX-02** fixture 与 adapter 同源检查 — dump_fixtures.py 的每个过滤字面量/口径必须与 adapters/tushare.py **机械对齐且可追溯**（当前 ST 注释『type uses 'ST', not 'S'』是人工注释非机械校验）；理想态：dump 脚本直接 import adapter 的 FORM。
- [ ] **T-MOCK-01** MockDataBackend 桩清单 — 恒空桩：`get_stock_industries`/`get_recent_listings`/`get_index_members`（`conftest.py:153-164`）——意味着 exclude_industries/exclude_new_stock/index_universe 在 fixture 下**零覆盖**；`get_st_map` 手写镜像非复用 generic_sql。
- [ ] **T-COV-01** 盲区清单（机制存在但无测试/只测不报错）— train_panel/train_guard 完整训练、export_model/_verify、ml_log=full、_apply_scaler 运行时兜底、model_exit 高阈值不触发、xs_zscore panel 物化、factor_eval --model、INV 级 ST/新股/行业断言。
- [ ] **T-ASSERT-01** 断言质量抽查 — 随机抽 10 个测试，分类：断言"结果正确"/断言"不报错"。后者占比高的文件列入加强清单。
- [ ] **T-E2E-01** 实盘冒烟流水线 — 每次引擎变更后：真实库跑示例策略 ≥1 个月窗口 → cross_validate 退出码 0 → replay 抽查 3 笔交易 → report 生成。**这是发现真实数据语义问题的最后防线，pytest 不能替代。**

---

## 11. 数据边界扫描矩阵

纵轴=边界维度（探针见附录 A），横轴=受影响功能。每格是潜在失效点；标 🛰 的功能边界扫描强制。

| # | 边界维度 | 探针 | 主要受影响功能 |
|---|---|---|---|
| B01 | ST 戴帽/摘帽/枚举变体 | A-03a/b/c/d | exclude_st🛰、涨跌停档位🛰、min_price |
| B02 | 停牌（行缺失 vs vol=0） | A-05 | 撮合跳过🛰、cap_by_volume🛰、rolling 窗口🛰、除权处理、估值冻结 |
| B03 | 涨跌停（板块×日期×ST） | A-06 | check_tradable🛰、条件单触发、事件因子 |
| B04 | 除权除息（大比例/连续/停牌除权） | A-07c/d、A-08 | 公司行为🛰、pre_close 口径🛰、trailing 锚点、stats 往返、ML 回合 |
| B05 | 新股（上市 60 日边界） | A-09 | exclude_new_stock🛰、neutralize 样本不足 |
| B06 | 指数调样/快照缺口 | A-10a/b | index_universe🛰、factor_universe、PIT 训练域 |
| B07 | 事件型稀疏列（起始日/未上榜 NaN） | A-12 | 事件计数因子🛰、validate_materialization NaN>5% |
| B08 | 亏损股 pe_ttm 分布 | A-15 | exclude_loss🛰 |
| B09 | 北交所裸代码（8xx/4xx 无后缀） | A-13 | exclude_boards🛰、涨跌停档位 |
| B10 | 行业映射缺失/历史变更 | A-14 | exclude_industries🛰、group_mean/neutralize、Brinson |
| B11 | 公告日≠交易日 | A-11 | 财报对齐因子🛰 |
| B12 | 键类型（INTEGER 日期列） | A-01 | 全部 SQL 查询🛰（静默查空） |
| B13 | 重复键 | A-02 | preload fail-fast（数据卫生） |
| B14 | 单位量纲（手/元/万元） | 静态 | 全部引用 vol/amount/seal 的因子 |
| B15 | NaN/±inf | 构造 | 算子 NaN 语义、ML scaler 护栏、BoolOp NaN→True |
| B16 | 日历边界（调休/区间外） | A-04 | warmup 前伸、get_historical_bars 切片、chunk 边界 |

---

## 12. 历史缺陷回归清单（每轮核查必过，防复发）

| ID | 缺陷 | 修复提交 | 回归验证 |
|---|---|---|---|
| R-HIS-01 | exclude_st 无法识别 ST（fixture 空转+同源假设） | 2026-08 | D-ST-01 三层全过 |
| R-HIS-02 | exclude_loss pe_ttm<=0 结构性失效 | 2012b39 | F-LOS-01 + A-15 |
| R-HIS-03 | buy 名单重复 symbol 账户腐化 | c961f92 | S-SEL-01 fail-fast |
| R-HIS-04 | preload get_universe/on_start 前视 | c961f92 | engine.py:208-210 锚定 |
| R-HIS-05 | target_value NaN/负值静默零成交 | f60b07c | S-SEL-02 校验 |
| R-HIS-06 | slippage_ticks 负值有利滑点 | f60b07c | E-CFG-01 |
| R-HIS-07 | 送转股不落库腐化 stats/Brinson/ML 四方 | 8593bfd | E-DB-01 + R-STATS-01 |
| R-HIS-08 | ret_from_entry hfq/裸价混用（meta v2→v3） | 93c18cc | M-STATE-01 |
| R-HIS-09 | extract_trade_pairs FIFO 失配/部分卖出 pnl 错 | 93c18cc | M-PAIRS-01 |
| R-HIS-10 | factor_eval warmup/坍缩/PIT 与引擎不同源 | 75274e6 | FA-DYN-01 |
| R-HIS-11 | Brinson 组合收益恒 0（结转/ffill/行业口径） | 2b42677 | RS-BRN-01 |
| R-HIS-12 | cross_validate trigger 硬编码误报/键名漂移 | c18c269 | CLI-XV-01 |
| R-HIS-13 | sweep 私有表 compare 不可用 | 5a1caeb | R-DB-02 |
| R-HIS-14 | dump_brinson 无 index 过滤多指数混入 | （批次 1） | CLI-BRN-01 |
| R-HIS-15 | compute_breadth warmup/YAML 重复键/同源列 | 16df190 | FA-BRD-01/FA-YAML-01 |
| R-HIS-16 | 空 universe 语义/重复键/键类型探针 | ac6ab5f | D-SQL-02/03/04 |

---

## 附录 A：真实数据探针速查

对 `/home/netube/aiwork/tushare_db/data/market.db` 执行（`sqlite3 <db> "<sql>"` 或 `uv run python`）。日期/代码列名以实际 schema 为准（先 `.tables` + `.schema <表>`）。

```sql
-- A-01 键类型：对每张被引用表
SELECT typeof(trade_date), typeof(ts_code) FROM stk_factor_pro LIMIT 1;   -- 期望 text/text

-- A-02 重复键
SELECT trade_date, ts_code, COUNT(*) c FROM stk_factor_pro GROUP BY 1,2 HAVING c>1 LIMIT 3;  -- 期望 0 行

-- A-03 ST 表语义（基线见 §0.4）
SELECT DISTINCT type FROM stock_st;                                       -- A-03a 基线仅 'ST'
SELECT trade_date, COUNT(*) FROM stock_st GROUP BY trade_date ORDER BY trade_date DESC LIMIT 5;  -- A-03b 日频快照
-- A-03c 摘帽案例：找曾出现但最近 30 日消失的 symbol
-- A-03d ST 日期是否全在交易日历内（列名以库为准）
SELECT COUNT(*) FROM stock_st s LEFT JOIN trade_cal t ON s.trade_date=t.cal_date WHERE t.cal_date IS NULL;

-- A-04 日历覆盖
SELECT MIN(cal_date), MAX(cal_date), COUNT(*) FROM trade_cal WHERE is_open=1;

-- A-05 停牌约定：主表有无 vol=0 / 缺行（对比日历×股票网格）
SELECT COUNT(*) FROM stk_factor_pro WHERE vol=0;

-- A-06 ST 票涨跌停覆盖：ST 名单 ∩ stk_limit 缺失率；ST 票 up_limit/pre_close 比值分布（应见 1.05）
-- 注意：stk_limit.pre_close 全 NULL（2026-08-02 实证），必须 JOIN stk_factor_pro 取 pre_close：
SELECT ROUND(ul.up_limit/f.pre_close,4), COUNT(*) FROM stk_limit ul
JOIN stock_st s ON s.trade_date=ul.trade_date AND s.ts_code=ul.ts_code
JOIN stk_factor_pro f ON f.trade_date=ul.trade_date AND f.ts_code=ul.ts_code
WHERE ul.trade_date='20240102' AND f.pre_close>0 GROUP BY 1 ORDER BY 2 DESC;  -- 基线：1.05 档为主

-- A-07 分红
SELECT DISTINCT div_proc FROM dividend;                                   -- A-07a
SELECT ts_code, ex_date, COUNT(*) c FROM dividend WHERE div_proc='实施' GROUP BY 1,2 HAVING c>1 LIMIT 5;  -- A-07b 拆行
SELECT * FROM dividend WHERE stk_div>=1.0 LIMIT 5;                        -- A-07c 大比例送转
-- A-07d 除权日无 bar 样本：dividend LEFT JOIN stk_factor_pro ON (ts_code, ex_date=trade_date) 空侧

-- A-08 adj_factor 跳变样本
-- （pandas：groupby ts_code 找相邻比值≠1 的日期）

-- A-09 新股边界：上市 55~65 日的票（相对回测区间）
-- A-10 指数成分
SELECT trade_date, COUNT(*) FROM index_weight WHERE index_code='000300.SH' GROUP BY trade_date ORDER BY trade_date DESC LIMIT 10;  -- A-10a 快照频率
-- A-10b 相邻快照日期间隔分布（缺口>45 天？）——julianday 对 TEXT 日期无效，用 Python 算：
--   rows = conn.execute("SELECT DISTINCT trade_date FROM index_weight WHERE index_code='000300.SH' ORDER BY trade_date")
--   基线（2026-08-02）：月频（28-34 天间隔为主+月初重复），最大间隔 36 天 < 45 天

-- A-11 公告日落点（2026-08-02 修正：bak_basic 是日频快照表，键为 trade_date 非 ann_date；
-- 基线：0 行非交易日，20180102→20260731 共 2063 个交易日）
SELECT COUNT(*) FROM bak_basic b LEFT JOIN trade_cal t ON b.trade_date=t.cal_date AND t.is_open=1 AND t.exchange='SSE' WHERE t.cal_date IS NULL;
-- 事件型表（repurchase/stk_holdertrade/pledge_detail 键 ann_date）同理可查；
-- 2026-08-02 基线：repurchase 101 行/stk_holdertrade 61 行在日历外（静默不可达）

-- A-12 事件型表起始日（pledge_detail/hm_list 无 trade_date 列，键为 ann_date）
SELECT MIN(trade_date) FROM limit_list_d; SELECT MIN(trade_date) FROM moneyflow_dc;
-- 2026-08-02 基线：limit_list_d 20191128；moneyflow_dc 20230911；top_list/top_inst/kpl_list/moneyflow 20180102

-- A-13 北交所裸代码
SELECT COUNT(*) FROM stock_basic WHERE ts_code NOT LIKE '%.BJ' AND (ts_code LIKE '8%' OR ts_code LIKE '4%');

-- A-14 行业映射缺失率
-- （index_member_all 与 stock_basic 的 ts_code 差集）

-- A-15 亏损股 pe_ttm 分布
SELECT COUNT(*) FROM stk_factor_pro f JOIN (SELECT DISTINCT ts_code FROM bak_basic WHERE eps<0) e ON f.ts_code=e.ts_code WHERE f.pe_ttm<=0;  -- 应极少
```

动态冒烟命令：

```bash
# E2E 冒烟（T-E2E-01）
uv run python scripts/run.py strategies/examples/rolling_ranker/config.yaml \
  --start 20240603 --end 20240628 --out /tmp/audit.db
uv run python scripts/cross_validate.py /tmp/audit.db; echo "exit=$?"
uv run python scripts/replay.py /tmp/audit.db --symbol <持仓股> --date <交易日>
uv run python scripts/report.py /tmp/audit.db --out /tmp/audit.html

# ST 探针（D-ST-01 动态层）
uv run python -c "from adapters.tushare import TushareBackend; b=TushareBackend(); \
m=b.get_st_map('20240101'); print(len(m), sorted(m.keys())[:2], len(m.get('20240102',set())))"
# 基线：624 ['20240102','20240103'] 117
```

---

## 附录 B：核查记录模板

```markdown
# ddup 核查记录 <日期> <核查人/agent>
范围：<全量 | 模块列表>    代码版本：<git rev-parse --short HEAD>

| ID | 静态 | 动态 | 边界 | 结论 | 证据（日志/SQL/命令输出指针） |
|----|------|------|------|------|------|
| D-ST-01 | ✅/❌/N/A | | | PASS/P0/P1/P2/P3 | |

发现的问题：
1. [P0] <ID> <现象> <证据> <file:line> <建议修复>
遗留：file:line 漂移修正 <n> 处；新增边界维度 <n> 个（已补入 §11）。
```
