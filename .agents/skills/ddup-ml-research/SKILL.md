---
name: ddup-ml-research
description: 训练、评估和消费 ddup panel/holding 模型，处理 models 声明、meta 契约、样本外验证及 ML 分数排错。
---

# ML 研究

先定义预测目标与策略消费方式，再选择 scope。ML 不要求先完成某个策略等级，
也不因普通规则表现差就自动成为下一步。完整模型/特征/meta 契约见
[ML 配置](../../../docs/ml_guide.md#configuration)。

## 训练与接入

1. 固定标签、特征可得日期、训练与最终验证窗口；holding 标签还需固定结果库/run_id。
2. 在 models 声明 artifact 与 features；首次训练允许 artifact 不存在。
   state 非空自动推导 holding，否则 panel，不手填另一套 scope 意图。
3. 按[训练入口](../../../docs/ml_guide.md#training)运行，检查样本量、embargo、
   训练段 scaler 与验证/测试分离。输出到独立模型版本路径。
4. 核对 ONNX/meta 配对、哈希和列序；meta version == 3。
   账户态仅 hold_days、ret_from_entry，后者按裸价及引擎成本/除权路径计算。
5. 通过 factor_specs、conditions.model_exit 或钩子自读分数消费；
   holding 分数不能放入 factor_specs。禁止策略自加载 ONNX 逐日推理。
6. 按[评估](../../../docs/ml_guide.md#evaluation)做独立样本外验证：panel 用 --model
   因子评估再回测；holding 查覆盖率、误报/漏报、触发与费用变化。

## 排错与边界

- 后变换针对输出分数；xs_rank/xs_zscore 后的分类分数不再是原概率。
- 缺失特征过半可导致无分数；先查 ml_predictions，再调阈值。
- 训练/推理共用物化函数，仍须对照数据窗口、指数成分域与因子版本。
- 模型两文件分两次替换，不是联合原子事务；导出中断后先检查哈希和配对。
- ML_EXIT 次日尝试执行，受撮合约束；失败不保证自动顺延。

交付模型版本/哈希、样本来源与切分、指标、交易影响和未验证范围。
诊断字段与报错路线见[观测](../../../docs/ml_guide.md#observability)；不把内部测试集
指标当作已完成最终样本外验收。
