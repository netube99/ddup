# ML 模型：声明、训练与消费

[文档入口](index.md) · [因子机制](factor_library.md) · [研究方法](factor_research.md)

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
<a id="configuration"></a>
## 2. 配置参考

### `models` 节（策略 YAML）

顶层为 mapping：`{模型名: 条目}`。模型名决定分数列名 `ml_<模型名>`。

| 键 | 类型 | 必需 | 说明 |
|----|------|------|------|
| `artifact` | str | **是** | ONNX 路径（相对策略 YAML 目录或绝对），训练输出写这里（首次训练可不存在）；加载时按 meta 的 `artifact_sha256` 校验文件字节，不符报错 |
| `meta` | str | 否 | meta 路径，缺省 = artifact 同名 `.meta.json` |
| `features` | dict | 训练引导 | 见下表；meta 存在时以 meta 为准，YAML 内联的 `factors` / `raw` 必须与 meta 一致或省略（`state` 不参与比对，scope 恒由 meta 的 `state_features` 决定）；meta 缺失时内联值即契约 |
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
hfq 收盘与之混用会被复权因子污染）。重放按引擎 `Holding.entry_price`
逐日路径：加仓日重算 cost/shares，现金分红除息日 rescale
entry ×= pre_close/(pre_close+每股税前红利)、cost 扣税后净额——
静态聚合买入均价无法表达这两类状态变化。

### `conditions.model_exit`（策略 YAML）

```yaml
conditions:
  model_exit:
    - {model: tb_guard, threshold: 0.6}
```

- list，每条必须含 `model` 键（引用已声明的模型，通常为 holding scope）
- `threshold` 缺省 0.5，必须 ∈ (0, 1)
- 持仓 bar 中 `ml_<model>` 分数 ≥ threshold 时生成 `ML_EXIT` 条件单；
  当日该持仓无分数（见下方引擎行为）则不触发
- 与 `stop_loss_pct` 等规则共存时按 ConditionBuilder 生成顺序求值，
  同一持仓首条触发的条件单成交后不再评估后续

### model_meta.json（v3，训练导出，勿手写）

| 键 | 说明 |
|----|------|
| `version` | `META_VERSION == 3`；v2 因 `ret_from_entry` 改为裸价口径被拒绝，需重新训练导出 |
| `name` / `scope` | 模型名 / 推导出的 scope（`panel` / `holding`） |
| `features` | `{factors, raw}` — 特征契约；输入向量列序 = factors + raw + state_features |
| `state_features` | 账户态特征列表；非空即 holding scope |
| `post_transform` | 分数截面后变换：`none`（缺省）/ `xs_rank` / `xs_zscore` |
| `label` | 标签描述：panel 为 `{"type": "xs_fwdret", "horizon": N}`；holding 为 `{"type": "trend_break", "lookahead": N}` |
| `train_window` | 训练窗口 `[start, end]` |
| `metrics` | 测试集评估指标（见[训练](#training)） |
| `n_train` / `n_test` | 切分后的样本数 |
| `scaler_mean` / `scaler_std` | StandardScaler 参数，推理时自动应用；维度与特征契约不符时加载期 `ValueError`（meta 与特征不一致 = 静默错分，请重新训练导出） |
| `artifact_sha256` | ONNX 文件字节哈希。写入用于版本追溯（随 runs.config_json 落盘）；加载 ModelSpec 时若 artifact 存在且 meta 含该键，重算 sha256 比对，不符 `ValueError`（篡改/半写 fail-fast） |
| `n_trees` | 可选：导出到 ONNX 的树数；early stopping 模型为 `best_iteration + 1`（onnxmltools 只导出最优轮次之前的树），非早停为 booster 总轮数 |
| `best_iteration` | 可选：early stopping 选中的轮次（0-based）；仅早停模型有。用于识别 artifact 是否被早停截断（对比宣称的树数/表现） |

<a id="training-cli"></a>
### `scripts/ml_train.py` 参数

| 参数 | 必需 | 说明 |
|------|------|------|
| `yaml`（位置参数） | 是 | 策略 YAML 配置路径 |
| `--model` | 是 | `models` 节中的模型名 |
| `--start` / `--end` | 是 | 训练窗口 YYYYMMDD |
| `--horizon` | 否 | panel 标签前瞻天数，缺省 5（也是 embargo 宽度） |
| `--db` | holding 必需 | holding scope 标签来源：含 trade_log 的回测结果库 |
| `--run-id` | 否 | 标签取用的 run_id；缺省取最新 `status='completed'` 的 run（无 completed 回退最新 run），多 run 时告警告知所选 |
| `--lookahead` | 否 | holding 标签前瞻天数，缺省 3（也是 embargo 宽度） |
| `--post-transform` | 否 | `none` / `xs_rank` / `xs_zscore`，写入 meta 作为训练/推理统一声明 |
| `-v` / `--verbose` | 否 | 调试日志 |

### 引擎行为要点

- panel 物化时机：因子物化之后、`factor_universe` 裁切之前——截面后变换的
  排名口径 = 因子计算全域，计算函数与训练侧共用；实际股票域仍须核对指数成分与加载窗口
- holding 求值时机：每日 `select` / `calc_conditions` 之前逐持仓注入；
  截面后变换口径 = 当日全部持仓
- 分数语义：回归模型输出预测值；分类模型输出正类概率
- 缺失特征与 ±inf 在 scaler 之后按 0 填充（标准化空间的 0 = 训练段均值，缺失/发散被
  解释为中性；训练侧先把 ±inf 归一为 NaN，两侧同口径）；缺失特征过半则**无分数**：holding 模型该持仓当天无分数
  （bar 中不出现 `ml_<name>` 键），panel 模型该行分数为 NaN（后续评分按缺失值规则处理），
  `model_exit` 不会触发
- 回测窗口与 meta `train_window` 重叠时引擎告警（样本内乐观偏差风险）；
  仅告警不阻断，walk-forward 复用模型属合法用法
- 引擎 config 键 `ml_log`：`"full"` 时 ml_predictions 表落盘全截面分数；
  缺省只落盘决策相关标的（持仓 + 当日买卖名单 + 条件买单）。

---
<a id="training"></a>
## 3. 训练与导出

```bash
# panel scope：标签 = horizon 日前向收益（hfq 口径）的每日截面 pct rank
.venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
    --model alpha_xs --start 20220101 --end 20250630 --horizon 5

# holding scope：标签 = 回测结果库 trade_log 中 TREND_BREAK 触发且净亏损的持仓回合
.venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
    --model tb_guard --start 20220101 --end 20250630 \
    --db results/baseline.duckdb --lookahead 3
```

holding 标签只消费单一 run 的 trade_log：多 run 结果库缺省取最新
completed run（`--run-id` 显式指定），同日公司行为（DIV/STK_DIV）
先于买卖处理，红利计入在持回合 pnl。

训练域：`filter_rules.index_universe` 成分在训练窗口内的并集；未配置则全市场。
配置了但窗口内解析不到任何成分快照（成分表未同步等）→ 打印警告并回退
全市场训练，不会静默改变训练域。面板向 start 之前延伸 warmup（按因子闭包推导）；窗口尾部缺少未来价格的标签不能作为有效样本。

切分纪律：

- 按交易日排序 80/20 切分，切点之前 horizon/lookahead 个交易日从训练集
  **剔除（embargo）**——标签窗口不跨切分点
- 特征标准化（StandardScaler）只在训练段拟合；early stopping 用训练段
  尾部 15% 的日期做验证集，测试集只用于评估
- 样本下限：panel ≥ 500 行；holding ≥ 100 行且正样本 ≥ 20，不足直接报错

评估输出：

- panel：每日截面 Spearman IC（mean / ICIR / 正值占比 / 有效天数）+
  十分层多空收益与单调性。指标与 `research/factor_eval` 同源，但**键名与
  分层形态有差异**（EDGE-16，跨工具对比时勿直接混用）：
  - 键名：训练侧 `btcore/ml/metrics.summarize_ic` 用 `ic_pos_ratio`，
    `research/factor_eval.summarize_ic` 用 `ic_positive_ratio`
    （`icir` 两侧同名同义）
  - 分层：训练侧 `layered_returns` 输出**各层平均前向收益**标量
    （`layer_mean` + `long_short`/`monotonic`，缺省 10 层）；
    `factor_eval.calc_layered_returns` 输出**各档累计收益曲线**
    （cumprod 序列，缺省 5 档），多空收益取末值之差
- holding：AUC / precision@0.5 / recall@0.5（正类 = 即将发生 TB 亏损离场）

产出物（写到 `artifact` 声明路径）：

- `<name>.onnx` — XGBoost ONNX 模型（panel 回归 / holding 二分类）
- `<name>.meta.json` — meta v3：特征契约 + scaler + 评估指标 + sha256（勿手写）

导出先写临时文件；提供非空校验样本时，比对原模型与 ONNX（含 scaler），
最大差异超过 1e-4 则拒绝发布。CLI 训练传入校验样本。
ONNX 与 meta 分别通过 os.replace 落位，单文件替换是原子的，两个文件不是联合事务；
两次替换之间中断可能留下不匹配的一对文件。加载时的哈希校验用于发现这种不一致。

meta 的 `artifact_sha256` 不只用于追溯：加载 ModelSpec 时若 meta 含该键且
artifact 存在，会重算文件 sha256 并比对，不符（篡改/半写）直接 `ValueError`。
首训引导（`require_meta=False`、artifact 尚不存在）跳过该校验。
<a id="bootstrap"></a>
## 4. 首次训练与消费

```yaml
models:
  alpha_xs:
    artifact: ml_model/alpha_xs.onnx
    features:
      factors: [mom20]   # 必须存在于所用因子库
      raw: [turnover_rate]
  tb_guard:
    artifact: ml_model/tb_guard.onnx
    features:
      factors: [mom20]
      state: [hold_days, ret_from_entry]
factor_specs:
  - {factor: ml_alpha_xs, weight: 1.0}
conditions:
  model_exit:
    - {model: tb_guard, threshold: 0.6}
```

以上是合并到完整策略 YAML 的片段。首次训练允许 artifact 不存在，但必须内联 features；
训练产出 ONNX 与 meta 后才能回测。引擎要求两文件存在，meta 版本不是 3、特征不一致、
scaler 维度不符或哈希不匹配都会拒绝加载。模型因子并入 materialize_only 闭包，
raw 并入请求列；holding 分数不能放入 factor_specs。

消费通道：panel 分数参与 factor_specs；持仓分数交给 conditions.model_exit；或在
策略钩子中读取 ml_<name> 自行解释。沿用 Strategy 的默认 ConditionBuilder 即可；
覆写 on_start/on_tick 时保留父类初始化/清理行为。策略不得自行加载 ONNX。

ML_EXIT 在 T 日决策生成，T+1 按开盘价尝试成交，遵守条件单价格、涨跌停、量 cap、
T+1 锁定约束。当天未成交的条件不会保证自动顺延；下一决策日重新计算。
trigger 为 ML_EXIT，附带 model 和 score。阈值属于策略，模型不自带强制交易意图。

<a id="evaluation"></a>
## 5. 评估与模型版本

1. 先明确标签含义：panel 是前向收益截面排名的回归预测，holding 是 TB 亏损回合预警。
   不把回归分数解释为事件概率，也不把分类分数视为已校准概率。
2. horizon/lookahead 与业务问题对应；最终检验窗口须晚于训练与调参窗口。
   内部 80/20 切分不替代独立样本外复测。
3. 后变换作用于**输出分数**，不是输入特征。none 保留原输出，xs_rank/xs_zscore
   改成当日相对位置；holding 截面是当日持仓，panel 是物化域。变换后的分类分数
   不再保有原概率含义，model_exit 阈值需要按新的分数语义解释。
4. 用 factor_eval.py --model 在样本外复测 panel IC/衰减，再固定交易规则做回测。
   holding 查分数覆盖、触发、误报和漏报，再看换手、磨损与收益影响。
5. 滚动重训保留每个窗口的 artifact/meta，分别运行；sweep 的模型路径使用绝对路径，
   不覆盖仍被历史实验引用的模型。

市场广度、行业特征和账户态可以组合，但模型不规定策略意图。只有所有输入同日相同，
才应预期所有股票输出相同；含个股特征时不能称为纯市场状态分数。

<a id="observability"></a>
## 6. 观测与排错

| 位置/现象 | 核对内容 |
| --- | --- |
| `runs.config_json` 的 `models_meta` | 每条只有 `name` / `scope` / `label` / `train_window` / `artifact_sha256`；完整策略 `config` 与生效的 `initial_capital` / `max_positions` 同在这一列。因子定义和因子库版本**不落盘**，追溯须对照当时的 `factors/` 库与 `factor_specs` |
| ml_predictions | run_id/date/symbol/model/score；ml_log=full 扩大 panel 落盘范围 |
| debug/replay | 快照 `bars_subset` 取自决策现场 bar dict，panel/holding 分数（`ml_*` 列）都往其中可见；分数落盘另查 ml_predictions |
| artifact/meta 缺失 | 首训内联 features 并训练；回测不能用占位文件 |
| features 不一致 | 与 meta 对齐，或用新路径重训；不要手写 meta 绕过契约 |
| 模型不触发 | 查缺失特征、分数覆盖、阈值与 scope，再查撮合拒单 |
| IC 高而收益差 | 核对样本外窗口、交易域、执行价格、持有期、费用与基准 |
| `post_transform` 未生效 | 只由训练参数写入 meta；在 `models` 条目里写同名字段不报错也不生效 |

模型推理、训练面板和账户特征复用机制能减少漂移；仍须核对数据快照、因子定义、股票域
和窗口。代码同源不等于不同实验的输入相同。

### 已知边界（当前实现）

以下行为有回归证据，不是文档笔误；遇到时按列出的影响判断，
不要假设相反语义（括号内为历史登记 ID，仅作定位符，不代表仍在 backlog）。

- **YAML 内联 `state` 不参与一致性校验**（`F-ML-05`）：meta 存在时只比对 `factors` / `raw`，
  YAML 里写错 `state` 不报错，scope 恒按 meta 的 `state_features` 推导。改 scope 必须重新
  训练导出，不能只改 YAML。
- **`post_transform` 只能来自 meta**：`models` 条目写同名键不报错、不生效，也无告警。
- **panel 标签按行位移**（`F-ML-06`）：`xs_forward_return` 用组内 `shift(-horizon)`；个股
  期内停牌缺行时，实际跨度可能多于 horizon 个市场交易日。稀疏面板下 horizon 不是严格
  日历意义上的“N 个交易日”。
