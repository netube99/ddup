---
name: ddup-experiment-design
description: 设计和执行 ddup 对照回测、参数扫描及样本外验证，控制变量、写库与结果可比性；用于实验和过拟合诊断。
---

# 回测实验

先读[实验方法](../../../docs/factor_research.md#experiments)，固定假设、对照、评价指标、
训练/选择窗口与最终检验窗口。优先单变量；极端值和反向假设用于区分解释，不是每次必跑项目。

## 执行

1. 记录 commit/diff、完整策略/因子配置、模型哈希、数据来源/窗口与费用口径。
   runs.config_json 不等于完整配置快照。
2. 单次实验用[run](../../../docs/cli_and_research.md#run)，扫描用
   [sweep](../../../docs/cli_and_research.md#sweep)。先用 --dry-run 检查组合数与字段路径。
3. run 必须显式 --out 才落盘。共享 DuckDB 不并发写；sweep 本身串行，
   独立进程需要不同结果库。报告路径也应防覆盖。
4. sweep 的列表索引形如 factor_specs.0.weight；base 相对工作目录解析。
   模型 artifact/meta 使用绝对路径，避免临时 YAML 改变相对路径含义。
5. 用[交叉验证](../../../docs/results_analysis.md#cross-validation)解释逐项告警，再对比收益、
   回撤、超额、换手、费用、持仓集中度与交易负担。测试错误与经济性差分开报告。
6. 在未用于选择参数的时间段检验方向和稳定性，保留失败实验；大量尝试增加选择偏差。

## 完成条件

回答预先声明的问题就结束，不自动继续网格细化。交付参数/DB/run_id、可比性条件、
样本内外差异和结论可信范围。没有独立检验窗口时明确写“尚未样本外验证”。
旧引擎结果若受计算修复影响，先判断是否要重跑；不能假设绝对收益错但相对排名仍可靠。
