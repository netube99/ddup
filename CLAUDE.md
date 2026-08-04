# CLAUDE.md — ddup

A 股日频量化策略回测引擎。Python 3.12+，uv + hatchling，核心依赖 pandas/numpy/numexpr/pyyaml。
完整架构见 `ARCHITECTURE.md`，开发规则见 `AGENTS.md`，设计文档入口 `docs/index.md`。

## 分层与目录

```
btcore/      机制/基础设施（勿随意修改）：engine.py 主循环、provider.py 前视门面、
             backend.py DataBackend ABC、strategy.py Strategy ABC、strategy_loader.py YAML 加载、
             factors/ 因子机制（ops 算子表/plan 物化规划/cse/library）、
             match/ 撮合（core 原语/conditions 条件单/manual 普通单，子模块互不 import）、
             ml/ ML 子系统（spec/dataset/runtime/trainer/conditions/export）、
             database.py 结果库、stats.py 统计纯函数、generic_sql.py 填表法后端
adapters/    用户后端（可编辑）：tushare.py = GenericSQLBackend 填表
factors/     用户因子 library.yaml（可编辑，纯 YAML 数据，无因子类）
strategies/  用户策略（可编辑）：examples/ selected/ exploring/ archive/
research/    研究工具库（无 CLI）：factor_eval/composite/attribution/report
scripts/     CLI 入口：run/report/compare/factor_eval/ml_train/sweep/replay/cross_validate
tests/       407 测试 + fixtures/*.parquet + test_invariants/（INV1-INV8）
```

依赖单向：btcore 不 import 用户层；engine.py 不被 btcore 内部 import；`scripts/check_anticorrupt.py` 强制。

## 关键入口

- `Engine.run(start, end)`（btcore/engine.py:113）：preload → 因子/ML 物化 → 逐日 step → 统计落库
- `Engine.step`（engine.py:291）：公司行为 → 撮合（manual → 条件卖 → 条件买）→ 结算 → 次日决策
- `Engine.compute_pending`（engine.py:404）：on_fills → on_tick → select → 校验 → calc_conditions
- `Strategy` ABC（btcore/strategy.py:8）：声明式属性 REQUIRED_FIELDS/FACTOR_SPECS/FILTER_RULES +
  钩子 get_universe/on_start/on_fills/on_tick/select/calc_conditions
- `strategy_loader.load_strategy(path)`（strategy_loader.py:147）：YAML → Strategy；
  策略模型 features 以 materialize_only 并入因子闭包（build_strategy :101）
- 因子：`ops.eval_op_expr`（factors/ops.py:367，_OPS 固定算子表）；
  `plan.build_factor_plan`（factors/plan.py:157）/ `materialize`（:273 两路供给：广度面板→主面板）
- 撮合：`match.conditions.exit_conditions`(:57)/`entry_conditions`(:166)；
  自定义条件单 `register_condition_handler`（match/conditions.py:23）
- ML：`ml/runtime.materialize_predictions`(:88) → `ml_<name>` 列；`ml/dataset.build_panel`(:22)
  训练与引擎同一物化函数链；meta v3 契约（ml/spec.py:85）
- 结果库：`database.init_backtest_db`（database.py:87），6 表多 run 累积 SQLite
- 统计：`stats.calculate_statistics`（stats.py:9，纯函数）

## 数据流（一天）

T-1 日 `compute_pending`：provider 前视锚点钳制（set_as_of/get_as_of）→ select(bars, snapshot, provider) 返回
{buy/sell/target_value/sell_shares/buy_weights/buy_conditions} → 逐持仓 calc_conditions。
T 日 `step`：corporate.adjust → 执行昨日 pending（涨跌停/量 cap 护栏）→ _settle 写库 → 再算次日。
首日信号在 prev_day 预计算，保证 T 信号 T+1 撮合。

## 约定（违反即 bug）

- 日期全仓 YYYYMMDD str；面板 MultiIndex(trade_date, symbol)
- 裸价（open/close）撮合估值，后复权（*_hfq = 裸价 × adj_factor）因子排名，不可混用
- T+1：买入当日 Holding.locked=True，次日解锁；条件单跳过锁定持仓
- 前视三重保护：因子因果物化 + provider 按 _as_of_date 钳制 + T 信号 T+1 撮合
- 可选能力缺失软回退（告警继续）；明确依赖缺失 fail-fast（ValueError）
- backend 能力与策略钩子均鸭子类型探测（getattr），缺则降级
- 引擎不内置因子（无 builtin.py）、Strategy ABC 无行为开关、_OPS 非注册表

## 命令

```bash
pytest tests/ -v                                        # 全部测试（fixtures，无需真实库）
ruff check btcore/ tests/ scripts/ research/ strategies/ factors/ adapters/
python scripts/check_anticorrupt.py                     # 反破坏 linter（提交前必过）
python scripts/run.py strategies/examples/rolling_ranker/config.yaml --start 20240603 --end 20240628
python scripts/report.py result.db --out report.html    # 单 run HTML 报告
python scripts/factor_eval.py mom20,vol_z --start 20240101 --end 20240630
python scripts/ml_train.py strategies/my_strategy/config.yaml --model alpha_xs --start 20220101 --end 20250630 --horizon 5
```

## 风格

Ruff line-length=100，规则 E/F/I/N/W，双引号；Python 3.12+ 类型标注；docstring 最小化；
代码与注释不用 emoji；编辑文件后检查同目录 AGENTS.md 是否受影响。
