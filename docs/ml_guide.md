# ML 子系统指南
# ML 子系统指南
ddup ML 子系统是引擎核心功能。**模型对引擎是抽象的打分公式**——引擎
不认识模型的意图（选股、离场、市场状态、板块热度……），只提供唯一、
因果的数据管线。意图永远写在策略里。

唯一的区分是**输入数据何时存在**（scope），由特征自动推导而非声明：
scope=panel 的模型在 preload 随因子批量物化为 `ml_<name>` 列；
scope=holding 的模型在决策时点逐持仓求值，分数注入持仓的 bar dict。
两种 scope 共用同一特征契约、同一物化函数链——训练与回测不存在
第二条会漂移的特征管线。

---

## 1. 架构

```
策略 YAML models 节（artifact + 特征契约）
     │
     ├─ 训练（scripts/ml_train.py）          ├─ 回测（Engine.run）
     │   ModelSpec(require_meta=False)        │   ModelSpec(require_meta=True)
     │   dataset.build_panel  ────────────────┤   engine preload
     │   = build_factor_plan + materialize    │   = build_factor_plan + materialize
     │   （与引擎同一组函数，逐列一致）          │   + scope=panel: 批量物化 ml_<name> 列
     │   labels → trainer → export            │   + scope=holding: 决策时点注入 bar
     │        ↓                               │        ↓
     │   <name>.onnx + <name>.meta.json ──────┘   策略自行解释分数
     │        （meta v2 = 特征契约）           （评分 / model_exit / 任意逻辑）
     └───────────────────────────────  ml_predictions 表 / debug 快照
```

**双 scope**（由 state_features 自动推导）：

| | panel（无账户态特征） | holding（含账户态特征） |
|---|---|---|
| 特征 | 纯行情列（因子 + raw 列） | 行情列 + 账户态（hold_days / ret_from_entry） |
| 求值 | preload 全面板批量 → `ml_<name>` 列 | `_compute_pending` 逐持仓 → 注入持仓 bar |
| 典型用途 | 截面打分、市场状态、板块热度 | 持仓预警、个性化离场 |
| 消费方式 | factor_specs / 策略自读 | calc_conditions / on_tick 自读 |

**依赖**：推理仅需 `onnxruntime`（惰性 import，未配置 models 的策略零依赖）；
训练额外需要 `xgboost` + `onnxmltools` + `scikit-learn`。

---

## 2. 快速开始

### 2.1 定义特征因子

ML 特征就是普通 library.yaml 因子，与选股因子共享算子库：

```yaml
# factors/library.yaml
factors:
  roc_5:
    expr: "roc(close_hfq, 5)"
    description: "5日动量"
  close_vs_ma20:
    expr: "close_hfq / ma(close_hfq, 20) - 1"
    description: "价格相对 MA20 偏离度"
```

### 2.2 配置策略 YAML

```yaml
models:
  alpha_xs:                              # 模型名 → 物化列 ml_alpha_xs
    artifact: ml_model/alpha_xs.onnx
    features:                            # 首次训练引导用；训练后以 meta 为准
      factors: [roc_5, close_vs_ma20]
      raw: [turnover_rate, pe_ttm]
    # post_transform: xs_rank            # 可选：分数列截面后变换

  tb_guard:                              # 含账户态特征 → holding scope
    artifact: ml_model/tb_guard.onnx

factor_specs:
  - factor: mom_z
    weight: 0.3
  - factor: ml_alpha_xs                  # panel 分数列按名引用，参与评分
    weight: 0.4

conditions:
  stop_loss_pct: 0.08
  model_exit:                            # 意图在策略侧：分数 ≥ 0.6 → ML_EXIT
    - {model: tb_guard, threshold: 0.6}
```

loader 行为（fail-fast，无静默）：

- 模型的 `features.factors` 自动并入因子闭包（`materialize_only`），
  享受 warmup 推导 / 列裁剪 / CSE / 广度面板全部机制；因子名不存在直接报错
- `features.raw` 自动并入 `REQUIRED_FIELDS` 向 backend 请求
- `factor_specs` 引用 `ml_*` 列时校验对应 panel scope 模型已声明；
  holding scope 模型不物化列（决策时点注入持仓 bar），引用即报错
- `conditions.model_exit` 引用的模型必须在 models 节已声明
- artifact / meta 缺失、YAML 与 meta 特征不一致 → 加载期报错

### 2.3 训练

```bash
# panel scope：标签 = horizon 日前向收益（hfq）的每日截面 pct rank
.venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
    --model alpha_xs --start 20220101 --end 20250630 --horizon 5

# holding scope：标签 = 回测 trade_log 的 TREND_BREAK + 净亏损
.venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
    --model tb_guard --start 20220101 --end 20250630 \
    --db results/baseline.db --lookahead 3
```

训练纪律（trainer.py）：

- 按 trade_date 排序 80/20 切分，切点前 horizon 个交易日从训练集
  **剔除（embargo）**——标签窗口不跨切分点
- StandardScaler 只在训练段拟合；early stopping 用训练段尾部 15%
- panel 评估：每日 Spearman IC / ICIR / 十分层多空（与
  research/factor_eval 同口径）；holding 评估：AUC / precision / recall

产出物（写到 artifact 声明路径）：

- `<name>.onnx` — XGBoost ONNX 模型
- `<name>.meta.json` — meta v2：特征契约 + scaler + 指标 + sha256

导出时自动做 sklearn-vs-ONNX 输出一致性校验（diff > 1e-4 则失败）。

### 2.4 策略消费

panel 分数列就是普通物化列，策略代码零 ML 感知：

```python
def select(self, bars, snapshot, provider):
    df = bars_to_df(bars)
    factor_df, score = eval_factor_specs(df, self.FACTOR_SPECS)  # ml_alpha_xs 已加权
    top = score.nlargest(self._top_k).index.tolist()
    return {"buy": top, "sell": [s for s in snapshot.holdings if s not in top]}
```

holding scope 的分数在决策时点注入持仓的 bar（`ml_tb_guard` 键），
策略自行解释——声明式用 ConditionBuilder 的 `model_exit` 规则：

```python
def on_start(self, provider, first_date, end_date=None):
    self._cb = ConditionBuilder(self.config.get("conditions"))

def calc_conditions(self, symbol, entry_price, bar, holding_days):
    return self._cb.calc(symbol, entry_price, bar, holding_days)
```

或完全自定义（读 `bar["ml_tb_guard"]` 实现任意逻辑：减仓、加条件单、
记日志……）。`ML_EXIT` 成交与 MANUAL 卖出同口径（次日开盘价），享受
条件单全部既有护栏（跌停顺延 / 成交量 cap / T+1 锁定）。

---

## 3. 配置参考

### 3.1 `models` 节（YAML）

| 键 | 类型 | 必需 | 说明 |
|----|------|------|------|
| `artifact` | str | **是** | ONNX 路径（相对策略目录或绝对），训练输出也写这里 |
| `meta` | str | 否 | meta 路径，缺省 = 同名 `.meta.json` |
| `features` | dict | 训练引导 | `{factors: [...], raw: [...], state: [...]}`；meta 存在时必须一致或省略 |

scope 由 `state_features` 自动推导（非声明）：含账户态特征 → holding。
旧版 `role` / `threshold` 键已废弃（role 仅告警忽略；阈值是策略意图，
写在 `conditions.model_exit` 或策略代码里）。

### 3.2 `conditions.model_exit`（YAML，意图出口）

```yaml
conditions:
  model_exit:
    - {model: tb_guard, threshold: 0.6}
```

持仓 bar 中 `ml_<model>` 分数 ≥ threshold 时生成 `ML_EXIT` 条件单。
列表可配多条；与 stop_loss_pct 等规则共存时按 ConditionBuilder 生成
顺序触发（首条触发即成交）。

### 3.3 model_meta.json（v2，训练导出，勿手写）

| 键 | 说明 |
|----|------|
| `version` | 固定 2 |
| `features` | `{factors, raw}` — 特征契约，feature_order = factors + raw + state |
| `state_features` | 账户态特征：`hold_days` / `ret_from_entry`（存在即 holding scope） |
| `post_transform` | 分数截面后变换：`none` / `xs_rank` / `xs_zscore` |
| `label` / `train_window` / `metrics` / `n_train` / `n_test` | 训练标注 |
| `scaler_mean` / `scaler_std` | StandardScaler 参数 |
| `artifact_sha256` | 模型文件哈希（版本追溯，写入 runs.config_json） |

### 3.4 引擎 config 键

| 键 | 说明 |
|----|------|
| `ml_log` | `"full"` 时 ml_predictions 落盘全截面分数；缺省只落盘决策相关标的（持仓+当日买卖名单） |

---

## 4. 标签与变换的选择

| 选择 | 推荐 | 理由 |
|------|------|------|
| panel 标签 | `xs_fwdret`（截面排名） | 消市场 beta，跨日可比；原始收益标签被 beta 主导 |
| 特征已含 XSEC 算子（`zscore(...)` 等） | `post_transform: none` | 因子表达式内已截面化，避免双重变换 |
| 原始量纲特征（turnover_rate 等 raw 列） | `post_transform: xs_rank` | 分数跨日可比，与 eval_factor_specs 的 rank 语义一致 |
| horizon | 与策略调仓周期一致 | 5 日调仓 → horizon=5 |

`post_transform` 的排名口径：panel scope = 因子计算域（factor_universe
裁切前），holding scope = 当日持仓截面；两者均训练与推理一致。

### 意图模式（引擎零新增概念的证明）

| 意图 | 实现 |
|------|------|
| 选股 alpha | panel 模型 + factor_specs 引用分数列 |
| 持仓预警离场 | holding 模型 + `conditions.model_exit` |
| 市场状态/风险 | panel 模型引用坍缩因子（广度特征）→ 分数同日全市场一致，select 中自读门控买侧 |
| 板块热度 | panel 模型引用 group 坍缩因子，分数按板块聚合后自读 |

后两者不需要任何新机制：市场/板块特征本来就是坍缩因子，模型只是
把它们组合成分数，解释权在策略。

---

## 5. 可观测性

| 工具 | ML 集成 |
|------|---------|
| `scripts/factor_eval.py --model x.onnx` | 模型分数物化后走与因子完全相同的 IC / IC 衰减 / 分层 / 相关性报告 |
| `scripts/cross_validate.py` | ML_EXIT 是登记触发类型，磨损审计通用 |
| `scripts/replay.py`（debug 模式） | 快照 bars_subset 含 `ml_*` 列——决策现场可见当日模型分数 |
| `scripts/compare.py` | runs.config_json 含 `models_meta`（名称/scope/标签/训练窗口/sha256），多 run 可区分模型版本 |
| `ml_predictions` 表 | `(run_id, date, symbol, model, score)`——SQL 直接查任意决策点的模型分数 |

滚动重训（walk-forward）不做进程内在线学习——用 sweep.py 多 run，
每 run 挂不同版本模型 artifact，结果库多 run 对比。

---

## 6. 文件结构

```
btcore/ml/
  spec.py       ModelSpec + meta v2 解析（fail-fast）
  runtime.py    ONNX 会话缓存 / 面板批量物化 / 账户态特征（训练推理同一公式）
  conditions.py ML_EXIT 条件单 handler
  dataset.py    训练面板构建（复用 factor_plan 同一物化路径）
  labels.py     xs_fwdret 截面标签 / trend_break 持仓标签
  trainer.py    时间切分 + embargo + XGBoost 训练
  metrics.py    IC / ICIR / 分层（与 research/factor_eval 同口径，自包含）
  export.py     ONNX 导出 + meta v2 + sklearn/ONNX 一致性校验

strategies/my_strategy/
  config.yaml              # models 节 + factor_specs 引用 ml_* 列 + conditions.model_exit
  strategy.py              # 零 ML 感知（panel）/ ConditionBuilder 委托（model_exit）
  ml_model/
    alpha_xs.onnx + alpha_xs.meta.json
```

---

## 7. 常见问题

**Q: 加载报 "缺少 meta 文件"？**
A: 模型还没训练。先在 YAML 内联 features，跑 `scripts/ml_train.py --model <name>`
导出 meta 后再回测。

**Q: "YAML 内联 features 与 meta 不一致"？**
A: meta 是特征契约的唯一事实源。重训换特征后删除 YAML 内联 features，
或改回一致。

**Q: 训练/推理特征对不齐？**
A: 不可能对不齐——训练面板与引擎 preload 调用同一组物化函数
（`tests/test_ml.py::test_dataset_matches_engine_panel` 逐列比对守住）。
报错时检查的是 meta.feature_cols 与因子库定义。

**Q: panel 模型 IC 很高但回测没超额？**
A: 检查 (1) post_transform 是否与特征截面化程度匹配（§4）；
(2) horizon 是否与调仓周期错配；(3) 用 `factor_eval.py --model` 在
样本外区间复测 IC 衰减。

**Q: holding 模型从不触发 / 触发过多？**
A: 调 `conditions.model_exit` 的 threshold（策略配置，改完直接回测，无需重训）。
先看 ml_predictions 表里该模型的分数分布再定阈值。

**Q: 缺 onnxruntime？**
A: `uv pip install onnxruntime`（推理）；训练另需
`uv pip install xgboost onnxmltools scikit-learn`。
未配置 models 的策略不需要任何 ML 依赖。
