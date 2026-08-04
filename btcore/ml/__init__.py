"""btcore.ml — ddup 机器学习子系统（引擎核心功能）。

模型对引擎是抽象的打分公式，引擎不认识其意图（选股/离场/市场状态），
只提供唯一、因果的数据管线：
  - scope=panel（无账户态特征）：preload 随因子物化批量求值，
    写 ml_<name> 列，与因子列同通道消费（factor_specs 评分 / 策略自读）。
  - scope=holding（含账户态特征）：决策时点逐持仓求值，分数注入持仓
    的 bar dict，策略在 calc_conditions / select 中自行解释。

训练侧（dataset/labels/trainer/export）与引擎共用 btcore.factors.plan
的同一物化路径——训练面板与回测面板逐列一致，无第二条物化管线。
"""
