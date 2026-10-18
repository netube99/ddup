---
name: ddup-strategy-craft
description: 创建或修改 ddup 策略 YAML/Python，落地选股、仓位、调仓、条件买卖和自定义退出；用于交易规则实现，不用于引擎重构。
---

# 策略实现

先按[五要素](../../../docs/strategy_patterns.md)写清决策周期、过滤与触发、退出优先级、
仓位、边界行为。用户已有规则时沿用；缺失细节影响交易行为时再澄清。
从最接近的 examples 选择实现，不要求逐级经过 L0–L4，ML 与策略复杂度分开判断。

## 实施顺序

1. 确认数据/因子已能供给，选定交易域和计算域。
2. 阅读[钩子](../../../docs/strategy_guide.md#hooks)与
   [select 七键协议](../../../docs/strategy_guide.md#select)，再写最小策略逻辑。
3. 配置[引擎参数](../../../docs/strategy_guide.md#config)、
   [过滤规则](../../../docs/strategy_guide.md#filters)与
   [声明式退出](../../../docs/strategy_guide.md#condition-config)。完整键表只在这些指南维护。
4. 需要限价/突破买入时读[条件买入](../../../docs/strategy_guide.md#buy-conditions)；
   特殊离场读[自定义 handler](../../../docs/strategy_guide.md#custom-conditions)。
5. 以五要素说明核对一个短窗的信号/成交，再按用户范围做实验；涉及计算修复时遵循项目回归协议。

## 必须核对的语义

- T 日信号 T+1 成交；整池当日无 bar 不调用决策钩子，因此 select 调用次数不一定等于交易日数。
- on_start/on_tick 覆写要保留父类初始化/清理或等效行为；calc_conditions 默认委托 ConditionBuilder。
- max_positions 不硬拦超额持仓；策略管理数量。buy_weights 是总资产目标比例，不是仅新买名单归一权重。
- target_value、普通名单与条件买入存在互斥要求；sell_shares 不能大于当前持股。
- 空 calc_conditions 不一定缺退出：先核对 sell、target_value 和其他明确路径。
- 未成交订单/条件单不保证顺延，按次日决策重建；同票多退出首条触发后停止。
- 排名用后复权，成交/成本/估值用裸价；不在策略自行加载 ONNX，也不绕过 provider 查询保护。

完成后交付规格、修改文件、实测行为和未验证边界，不自动改精选策略、基线或真实账本。
更详细的滑点、涨跌停、量 cap、零股规则查[执行参考](../../../docs/strategy_guide.md#execution)。
