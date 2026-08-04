---
name: ddup-ml-research
description: ddup ML 模型研究全流程：何时上 ML（与 L0-L4 正交）、models 节声明与 meta v3 契约、panel/holding 双 scope 语义与限制、ml_train 训练命令、factor_specs/model_exit/自读三条消费通道、样本外 factor_eval 复测与 ml_predictions 观测、禁止策略自加载 ONNX。训练/评估/消费 ML 打分模型、使用 ml_ 列或 model_exit 时使用。
---

# ddup ML 模型研究

ML 是引擎的"意图中性打分公式"插槽：引擎只管数据管线唯一性与因果正确，分数解释权属策略。细节见 `docs/ml_guide.md`。

## 何时上 ML（正交于 L0-L4，先完成阶梯验证再加）

- 线性组合 IC 已饱和、怀疑非线性交互 → panel 模型（选股 alpha）
- 需要持仓级预警（"这只持仓要走坏"）→ holding 模型 + model_exit
- 需要市场状态/板块热度打分 → panel 模型引用坍缩因子自读门控
- 否则不要上 ML：L0-L4 机制未吃透时 ML 只会放大过拟合

**术语消歧**：`strategies/examples/multi_model/` 是多套因子投票+状态机，**不是 ML**；ML 模型 = YAML models 节声明的 ONNX。

## models 节（btcore/ml/spec.py 唯一解析源）

```yaml
models:
  alpha_xs:
    artifact: models/alpha_xs.onnx      # 必需；相对策略目录；**文件必须已存在**（首训前 touch 占位）；训练导出覆盖同路径
    # meta: 缺省 = artifact 同名 .meta.json；version 必须 == 3，缺失/不符加载期报错
    features:                            # 仅首次训练引导（meta 存在时以 meta 为准，不一致报错）
      factors: [mom20, vol_z, rsi_z]     # 因子名 → 以 materialize_only 并入因子物化闭包
      raw: [turnover_rate]               # 后端原始列 → 并入 REQUIRED_FIELDS
      # state: [hold_days, ret_from_entry]  # 账户态特征；**写了 → holding scope，不写 → panel scope**
```
- scope 不声明，由 state_features 自动推导；`role` 键已废弃（仅 warning）
- `post_transform`（none/xs_rank/xs_zscore）**只能从 meta 读，YAML 写无效**；改它用 ml_train `--post-transform`

## 双 scope 语义（engine.py）

| | panel | holding |
|---|---|---|
| 物化时机 | preload 因子物化后、因子域裁切前，全面板批量推理写 `ml_<name>` **列** | 每日决策时点（on_fills/on_tick/select 前）对全部持仓批量推理，注入持仓 bar dict 的 `ml_<name>` 键 |
| 输入 | 仅行情列（factors+raw），无账户态 | 行情 + 账户态（hold_days、ret_from_entry 裸价口径，引擎统一公式） |
| 消费 | factor_specs 引用 / select 自读列 / factor_eval `--model` 评估 | conditions.model_exit / 策略自读 bar["ml_<name>"] |
| 限制 | — | **factor_specs 引用即加载报错**；不物化面板列；debug 快照无其分数 |
| 缺失语义 | 特征缺失 scaler 后填 0；缺失过半 → 分数 NaN → rank 末位 | 缺失过半 → 无分数（None），model_exit 不触发 |

分数语义：回归=预测值；分类=正类概率。截面后变换口径：panel=因子计算全域，holding=当日持仓截面（post_transform≠none 时 model_exit 单持仓 xs_rank 恒 1.0，加载期有 warning——阈值无意义）。

## 训练（scripts/ml_train.py，需真实数据库）

```bash
# panel（截面选股模型）
python scripts/ml_train.py strategies/my_strategy/config.yaml --model alpha_xs \
    --start 20220101 --end 20250630 --horizon 5 [-v]
# holding（持仓预警模型；--db = 标签来源的历史回测库，必需）
python scripts/ml_train.py strategies/my_strategy/config.yaml --model exit_guard \
    --start 20220101 --end 20250630 --db results/base_run.db --lookahead 3
```
- 标签：panel = horizon 日前向收益 close_hfq 截面 pct rank；holding = trade_log 回合重构，正样本=TREND_BREAK 且净亏损、距卖出∈[1,lookahead]
- holding 标签只消费单一 run：`--run-id` 显式指定，缺省取最新 completed run（无 completed 回退最新 run，多 run 时 warning）；同日公司行为（DIV/STK_DIV 盘前）先于买卖，红利计入在持回合 pnl，实盘账本 ADJUST 审计行跳过
- 训练域 = filter_rules.index_universe 成分并集 + PIT 过滤（未配则全市场）；80/20 时间切分 + embargo（切点前 horizon/lookahead 日剔除）；scaler 仅训练段拟合
- 产出：`<name>.onnx` + `<name>.meta.json`（v3：特征契约/scaler/指标/train_window/artifact_sha256）；导出强制 sklearn vs ONNX 一致性 ≤1e-4
- 样本下限：panel ≥500 行；holding ≥100 且正样本 ≥20（不足报错）
- 指标：panel = 日截面 Spearman IC/ICIR/胜率 + 十分层多空单调性；holding = AUC/precision/recall

## 消费三通道

1. **factor_specs 引用**（仅 panel）：`{factor: ml_alpha_xs, weight: 1.0, ascending: false}`——与普通因子同权参与评分
2. **conditions.model_exit**：YAML `conditions: {model_exit: [{model: exit_guard, threshold: 0.5}]}`；持仓 bar 分数 ≥threshold → ML_EXIT 条件单，**T+1 开盘价成交**（跌停顺延/量 cap/T+1 护栏全套）；trade_log trigger="ML_EXIT"（model/score 只在日志与快照，不回写 trade_log）
3. **策略自读**：panel 读 bars 列；holding 读 bar["ml_<name>"] 自定义逻辑

## 样本外复测与观测（训练完必做）

- `python scripts/factor_eval.py --model models/alpha_xs.onnx --start <样本外> --end ... [--universe CSI500]`：复用引擎同一物化链评 IC/分层（仅 panel）；训练段指标不算数
- holding 模型：先查 `ml_predictions` 表分数分布再定 threshold，不要拍脑袋 0.5
- `ml_predictions` 表：缺省只落持仓+当日买卖名单标的；config `ml_log: "full"` 落全截面
- 版本追溯：runs.config_json.models_meta（含 artifact_sha256）；回测窗口与 meta.train_window 重叠 → 启动 warning（walk-forward 合法，不阻断）
- 滚动重训：无在线学习；sweep 挂不同 artifact 版本对比

## 禁令（架构级，违反=静默错分）

- **策略自行加载 ONNX 逐日推理**：第二条数据管线必漂移；账户态特征只有引擎决策时点能算；缺失/截面口径无法自对齐；绕过 meta v3 fail-fast 与 ml_predictions 观测
- YAML models 节写 post_transform（无效，只从 meta 读）
- holding 模型列写进 factor_specs（加载报错）
- 用训练窗口内 IC 宣称模型有效（必须样本外 factor_eval 复测）

训练-引擎一致性由架构保证：build_panel 逐行复刻引擎 preload 物化链（同源函数，无第二条管线），feature_order/scaler/缺失口径同一契约——所以样本外 factor_eval 的 IC 与回测内行为同口径。
