# ML 子系统指南

## 1. 基本概念

**模型经 YAML `models` 节声明，引擎在加载期完成校验与挂接；策略代码永远不
自行加载 ONNX、不逐日手写推理循环。** 唯一的区分是输入数据何时存在（scope），
scope 不由用户声明，由特征自动推导：特征里含账户态特征（`state`）→
holding，否则 → panel。

| | panel（无账户态特征） | holding（含账户态特征） |
|---|---|---|
| 特征 | 纯行情列（因子 + backend raw 列） | 行情列 + 账户态（`hold_days` / `ret_from_entry`） |
| 何时算分 | preload 阶段随因子批量物化一次 | 每日决策时点（`select` / `calc_conditions` 之前）逐持仓求值 |
| 分数出现在哪 | 面板新列 `ml_<name>`，与普通因子列同通道 | 注入该持仓的 bar dict（键 `ml_<name>`） |
| 消费方式 | `factor_specs` 按名引用参与评分，或策略自读该列 | `conditions.model_exit` 声明式规则，或策略代码自读 |
| 典型用途 | 截面打分、市场状态、板块热度 | 持仓预警、个性化离场 |

**训练与回测同源**：训练脚本构建面板调用与引擎 preload 完全相同的一组物化
函数，特征面板逐列一致，不存在第二条会漂移的特征管线；账户态特征的计算
公式训练侧重放与引擎推理共用同一定义。模型输入向量的列序固定为
`factors + raw + state_features`，由训练导出的 meta 文件锁定。

**依赖**：推理仅需 `onnxruntime`（惰性加载，未配置 `models` 的策略零依赖，
配置了但缺包则首次推理时报错）；训练额外需要 `xgboost` + `onnxmltools` +
`scikit-learn`。

---

## 2. 快速开始

### 2.1 定义特征因子

ML 特征就是普通因子库因子，与选股因子共享算子库：

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
  alpha_xs:                              # 模型名 → 分数列 ml_alpha_xs
    artifact: ml_model/alpha_xs.onnx     # 相对策略目录；训练输出也写这里
    features:                            # 首次训练引导用；训练后以 meta 为准
      factors: [roc_5, close_vs_ma20]
      raw: [turnover_rate, pe_ttm]

  tb_guard:                              # features 含 state → holding scope
    artifact: ml_model/tb_guard.onnx
    features:
      factors: [roc_5, close_vs_ma20]
      state: [hold_days, ret_from_entry]

factor_specs:
  - factor: mom_z
    weight: 0.3
  - factor: ml_alpha_xs                  # panel 分数列按名引用，参与评分
    weight: 0.4

conditions:
  stop_loss_pct: 0.08
  model_exit:                            # 分数 ≥ 0.6 → 生成 ML_EXIT 条件单
    - {model: tb_guard, threshold: 0.6}
```

加载期校验（全部 fail-fast，无静默降级）：

- `artifact` 必填、必须是已存在的 `.onnx` 文件；meta 缺失（引擎路径）、
  meta 版本不是 2、YAML 内联 features 与 meta 不一致 → 直接报错
- 模型的 `features.factors` 自动并入因子闭包统一物化（不参与评分），
  享受 warmup 推导 / 列裁剪等全部既有机制；因子名不在因子库中直接报错
- `features.raw` 自动并入引擎向 backend 请求的列
- `factor_specs` 引用 `ml_*` 列时校验对应 **panel** scope 模型已声明；
  holding scope 模型不物化列（分数在决策时点注入持仓 bar），引用即报错
- `conditions.model_exit` 引用的模型必须在 `models` 节已声明

注意：模型首次训练前 `artifact` 路径也必须已存在（训练导出会覆盖它），
首次训练前先放一个占位文件：`touch ml_model/alpha_xs.onnx`。

### 2.3 训练

```bash
# panel scope：标签 = horizon 日前向收益（hfq 口径）的每日截面 pct rank
.venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
    --model alpha_xs --start 20220101 --end 20250630 --horizon 5

# holding scope：标签 = 回测结果库 trade_log 中 TREND_BREAK 触发且净亏损的持仓回合
.venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
    --model tb_guard --start 20220101 --end 20250630 \
    --db results/baseline.db --lookahead 3
```

训练域：`filter_rules.index_universe` 成分在训练窗口内的并集；未配置则全市场。
面板自动向 start 之前延伸 warmup 天数（按因子闭包推导），标签窗口不会缺数据。

切分纪律：

- 按交易日排序 80/20 切分，切点之前 horizon/lookahead 个交易日从训练集
  **剔除（embargo）**——标签窗口不跨切分点
- 特征标准化（StandardScaler）只在训练段拟合；early stopping 用训练段
  尾部 15% 的日期做验证集，测试集只用于评估
- 样本下限：panel ≥ 500 行；holding ≥ 100 行且正样本 ≥ 20，不足直接报错

评估输出：

- panel：每日截面 Spearman IC（mean / ICIR / 正值占比 / 有效天数）+
  十分层多空收益与单调性，与 `research/factor_eval` 同口径
- holding：AUC / precision@0.5 / recall@0.5（正类 = 即将发生 TB 亏损离场）

产出物（写到 `artifact` 声明路径）：

- `<name>.onnx` — XGBoost ONNX 模型（panel 回归 / holding 二分类）
- `<name>.meta.json` — meta v3：特征契约 + scaler + 评估指标 + sha256（勿手写）

导出时自动做 sklearn 原始模型 vs 导出 ONNX 的输出一致性校验（含 scaler 路径，
max diff > 1e-4 则导出失败）。

### 2.4 策略消费

panel 分数列就是普通物化列，策略代码零 ML 感知：

```python
def select(self, bars, snapshot, provider):
    df = bars_to_df(bars)
    factor_df, score = eval_factor_specs(df, self.FACTOR_SPECS)  # ml_alpha_xs 已加权
    top = score.nlargest(self._top_k).index.tolist()
    return {"buy": top, "sell": [s for s in snapshot.holdings if s not in top]}
```

holding 分数在决策时点注入持仓 bar，声明式消费用 ConditionBuilder：

```python
def on_start(self, provider, first_date, end_date=None):
    self._cb = ConditionBuilder(self.config.get("conditions"))

def calc_conditions(self, symbol, entry_price, bar, holding_days):
    return self._cb.calc(symbol, entry_price, bar, holding_days)
```

或完全自定义——在 `select` / `calc_conditions` / `on_tick` 里读
`bar["ml_tb_guard"]` 实现任意逻辑（减仓、加条件单、记日志……）。

`ML_EXIT` 条件单 T+1 以开盘价成交，与 MANUAL 卖出同磨损口径，享受条件单
全部既有护栏（开盘价非法顺延 / 跌停顺延 / 成交量 cap / T+1 锁定跳过），
trade_log 的 trigger 记为 `ML_EXIT` 并附带 model 与 score。

---

## 3. 配置参考

### 3.1 `models` 节（策略 YAML）

顶层为 mapping：`{模型名: 条目}`。模型名决定分数列名 `ml_<模型名>`。

| 键 | 类型 | 必需 | 说明 |
|----|------|------|------|
| `artifact` | str | **是** | ONNX 路径（相对策略 YAML 目录或绝对），必须已存在；训练输出也写这里 |
| `meta` | str | 否 | meta 路径，缺省 = artifact 同名 `.meta.json` |
| `features` | dict | 训练引导 | 见下表；meta 存在时以 meta 为准，YAML 内联值必须一致或省略 |
| `role` | str | 否 | **已废弃**：仅告警忽略。scope 由 state_features 推导，阈值写在策略侧 |

`features` 子键：

| 子键 | 说明 |
|------|------|
| `factors` | 因子库因子名列表，并入因子闭包物化 |
| `raw` | backend 原始列名列表（如 `turnover_rate`、`pe_ttm`），并入请求列 |
| `state` | 账户态特征列表，支持 `hold_days`、`ret_from_entry`；非空即 holding scope |

账户态特征定义（训练/推理同一公式）：`hold_days` = 引擎维护的持仓交易日数
（成交当日 decision 时点为 1，逐交易日 +1；训练侧重放按市场交易日位置
计算，与引擎逐日一致）；`ret_from_entry` = 当日裸收盘 / 买入均价 − 1
（裸价口径 = 账户市值盈亏，现金分红另行入账不计入；买入均价是裸成交价，
hfq 收盘与之混用会被复权因子污染）。

### 3.2 `conditions.model_exit`（策略 YAML）

```yaml
conditions:
  model_exit:
    - {model: tb_guard, threshold: 0.6}
```

- list，每条必须含 `model` 键（引用已声明的模型，通常为 holding scope）
- `threshold` 缺省 0.5，必须 ∈ (0, 1)
- 持仓 bar 中 `ml_<model>` 分数 ≥ threshold 时生成 `ML_EXIT` 条件单；
  当日该持仓无分数（见 §3.5）则不触发
- 与 `stop_loss_pct` 等规则共存时按 ConditionBuilder 生成顺序求值，
  同一持仓首条触发的条件单成交后不再评估后续

### 3.3 model_meta.json（v3，训练导出，勿手写）

| 键 | 说明 |
|----|------|
| `version` | 固定 3（v2 因 ret_from_entry 改为裸价口径被拒绝，需重新训练导出） |
| `name` / `scope` | 模型名 / 推导出的 scope（`panel` / `holding`） |
| `features` | `{factors, raw}` — 特征契约；输入向量列序 = factors + raw + state_features |
| `state_features` | 账户态特征列表；非空即 holding scope |
| `post_transform` | 分数截面后变换：`none`（缺省）/ `xs_rank` / `xs_zscore` |
| `label` | 标签描述：panel 为 `{"type": "xs_fwdret", "horizon": N}`；holding 为 `{"type": "trend_break", "lookahead": N}` |
| `train_window` | 训练窗口 `[start, end]` |
| `metrics` | 测试集评估指标（见 §2.3） |
| `n_train` / `n_test` | 切分后的样本数 |
| `scaler_mean` / `scaler_std` | StandardScaler 参数，推理时自动应用 |
| `artifact_sha256` | ONNX 文件哈希（版本追溯，随 runs.config_json 落盘） |

### 3.4 `scripts/ml_train.py` 参数

| 参数 | 必需 | 说明 |
|------|------|------|
| `yaml`（位置参数） | 是 | 策略 YAML 配置路径 |
| `--model` | 是 | `models` 节中的模型名 |
| `--start` / `--end` | 是 | 训练窗口 YYYYMMDD |
| `--horizon` | 否 | panel 标签前瞻天数，缺省 5（也是 embargo 宽度） |
| `--db` | holding 必需 | holding scope 标签来源：含 trade_log 的回测结果库 |
| `--lookahead` | 否 | holding 标签前瞻天数，缺省 3（也是 embargo 宽度） |
| `--post-transform` | 否 | `none` / `xs_rank` / `xs_zscore`，写入 meta 作为训练/推理统一声明 |
| `-v` / `--verbose` | 否 | 调试日志 |

### 3.5 引擎行为要点

- panel 物化时机：因子物化之后、`factor_universe` 裁切之前——截面后变换的
  排名口径 = 因子计算全域，与训练面板口径一致
- holding 求值时机：每日 `select` / `calc_conditions` 之前逐持仓注入；
  截面后变换口径 = 当日全部持仓
- 分数语义：回归模型输出预测值；分类模型输出正类概率
- 缺失特征在 scaler 之后按 0 填充（标准化空间的 0 = 训练段均值，缺失被
  解释为中性）；缺失特征过半则**无分数**：holding 模型该持仓当天无分数
  （bar 中不出现 `ml_<name>` 键），panel 模型该行分数为 NaN（截面排名末位），
  `model_exit` 不会触发
- 回测窗口与 meta `train_window` 重叠时引擎告警（样本内乐观偏差风险）；
  仅告警不阻断，walk-forward 复用模型属合法用法
- 引擎 config 键 `ml_log`：`"full"` 时 ml_predictions 表落盘全截面分数；
  缺省只落盘决策相关标的（持仓 + 当日买卖名单 + 条件买单）。

---

## 4. 标签与后变换的选择

| 场景 | 推荐 | 理由 |
|------|------|------|
| panel 标签 | 缺省的 `xs_fwdret`（前向收益截面 pct rank ∈ (0,1]） | 消市场 beta，跨日可比；原始收益标签被 beta 主导 |
| 特征已含截面算子（`zscore(...)` 等） | `--post-transform none` | 因子表达式内已截面化，避免双重变换 |
| 原始量纲特征（turnover_rate 等 raw 列） | `--post-transform xs_rank` | 分数跨日可比，与 eval_factor_specs 的 rank 语义一致 |
| horizon | 与策略调仓周期一致 | 5 日调仓 → `--horizon 5` |
| lookahead | 期望提前预警的交易日数 | holding 标签 = 距 TB 亏损离场 ≤ lookahead 个交易日 |

后变换口径：`xs_rank` 为逐日截面 pct rank ∈ (0,1]；`xs_zscore` 为逐日截面
z-score（截面标准差≈0 时置 0）。panel scope 的截面 = 因子计算全域
（factor_universe 裁切前），holding scope 的截面 = 当日持仓；训练与推理一致。

### 意图模式（引擎零新增概念）

| 意图 | 实现 |
|------|------|
| 选股 alpha | panel 模型 + `factor_specs` 引用分数列参与加权评分 |
| 持仓预警离场 | holding 模型 + `conditions.model_exit` |
| 市场状态/风险 | panel 模型引用坍缩因子（市场广度特征）→ 分数同日全市场一致，`select` 中自读门控买侧 |
| 板块热度 | panel 模型引用 group 坍缩因子，分数按板块聚合后自读 |

后两者不需要任何新机制：市场/板块特征本来就是坍缩因子，模型只是把它们
组合成分数，解释权在策略。

---

## 5. 可观测性

| 工具 | ML 集成 |
|------|---------|
| `scripts/factor_eval.py --model x.onnx` | 模型分数物化为 `ml_<name>` 列后走与因子完全相同的 IC / IC 衰减 / 分层 / 相关性报告；仅支持 panel scope |
| `scripts/cross_validate.py` | `ML_EXIT` 是登记的合法触发类型，交易审计通用 |
| `scripts/replay.py`（debug 模式） | 决策快照的 bars_subset 含 `ml_*` 列——决策现场可见当日模型分数 |
| 结果库 runs 表 | `config_json` 含 `models_meta`（每个模型的 name / scope / label / train_window / artifact_sha256），多 run 可区分模型版本 |
| 结果库 ml_predictions 表 | `(run_id, date, symbol, model, score)`——SQL 直接查任意决策点的模型分数；落盘范围见 `ml_log`（§3.5） |

滚动重训（walk-forward）不做进程内在线学习：用 sweep.py 跑多 run，每 run
挂不同版本的模型 artifact，在结果库中多 run 对比。

---

## 6. 策略侧文件结构

```
strategies/my_strategy/
  config.yaml              # models 节 + factor_specs 引用 ml_* 列 + conditions.model_exit
  strategy.py              # 零 ML 感知（panel）/ ConditionBuilder 委托（model_exit）
  ml_model/
    alpha_xs.onnx          # 训练导出（artifact 声明路径）
    alpha_xs.meta.json     # 特征契约 + scaler + 指标（meta v3）
```

---

## 7. 常见问题

**Q: 加载报「artifact 不存在」？**
A: `artifact` 路径必须已存在，包括首次训练前。先 `touch` 一个占位文件
（训练导出会覆盖），再跑训练。

**Q: 加载报「缺少 meta 文件」？**
A: 模型还没训练。在 YAML 内联 `features` 做引导，跑
`scripts/ml_train.py --model <name>` 导出 meta 后再回测。

**Q: 报「YAML 内联 features 与 meta 不一致」？**
A: meta 是特征契约的唯一事实源。重训换特征后删除 YAML 内联 `features`，
或改回与 meta 一致。

**Q: 训练/推理特征会不会对不齐？**
A: 不会——训练面板与引擎 preload 调用同一组物化函数，账户态特征共用同一
公式，输入列序由 meta 锁定。要检查的是特征因子在因子库中的定义本身。

**Q: panel 模型 IC 很高但回测没超额？**
A: 检查 (1) `post_transform` 是否与特征截面化程度匹配（§4）；
(2) horizon 是否与调仓周期错配；(3) 用 `factor_eval.py --model` 在
样本外区间复测 IC 衰减。

**Q: holding 模型从不触发 / 触发过多？**
A: 调 `conditions.model_exit` 的 `threshold`（纯策略配置，改完直接回测，
无需重训）。先查 ml_predictions 表看该模型的分数分布再定阈值；从不触发
也可能是持仓当天缺失特征过半导致无分数。

**Q: 在 YAML models 节里写 `post_transform` 有效吗？**
A: 无效。`post_transform` 由训练侧确定并写入 meta（用
`--post-transform` 指定），引擎只从 meta 读取。

**Q: 缺 onnxruntime？**
A: `uv pip install onnxruntime`（推理）；训练另需
`uv pip install xgboost onnxmltools scikit-learn`。
未配置 `models` 的策略不需要任何 ML 依赖。
