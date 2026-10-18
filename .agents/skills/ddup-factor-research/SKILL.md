---
name: ddup-factor-research
description: 设计和评估 ddup YAML 因子，分析 IC、分层、衰减、相关性、多因子合成及市场广度；用于因子研究或因子计算口径排错。
---

# 因子研究

先读[数据列与伪列](../../../docs/factor_library.md#columns)和与目标相关的
[后端对齐约束](../../../docs/backend_guide.md#alignment)，确认字段、单位、可得日期与股票域。

## 从假设到证据

1. 明确预测对象、方向、持有期和价格口径；排名通常用后复权，财报按公告日对齐。
2. 按[定义/表达式](../../../docs/factor_library.md#definition)写 YAML，用
   [算子表](../../../docs/factor_library.md#operators)组合已有机制。不要注册新因子类或运行时算子。
3. 核对依赖闭包、warmup、where 与缺失值；布尔计数按
   [显式算术化](../../../docs/factor_library.md#boolean-score)处理。
4. 用[评估命令](../../../docs/cli_and_research.md#factor-eval)固定窗口/股票域/前瞻期，
   查看 IC、分层、衰减、相关性和覆盖率；再用独立时间段复测。
5. 多因子合成先读[合成 API](../../../docs/factor_research.md#composite)。IC 权重的
   延迟须覆盖标签 horizon；研究 zscore 合成与策略百分位排名不是同一得分。

## 特殊路径

- collapse/广度先读[物化域](../../../docs/factor_library.md#materialization)：全市场标量
  无横截面区分力；group_mean 在行业间仍可不同，不能笼统宣布所有 collapse 的 IC 都为零。
- compute_breadth 的 group_mean 返回行业等权聚合标量，不是引擎广播后的行业分组面板。
  分块查询保留完整历史前缀，不保证内存随 chunk_size 有界。
- 明确声明的伪列、因子或原始字段缺失应定位数据依赖，不能以填零绕过。
- IC 异常先核对 forward 标签、next-open 对齐、股票域与 PIT；共享计算函数不证明样本域相同。

交付表达式/版本、数据条件、覆盖、样本内外证据及是否进入策略实验。
分层曲线是因子诊断，不是可直接交易的组合净值；不根据一个 IC 阈值宣称可实盘。
