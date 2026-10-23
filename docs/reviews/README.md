# 历史审查与维护记录

本目录保存当时的检查范围、发现与证据，不把历史结论当作当前版本的验收。
当前规则见 [开发收敛协议](../review_protocol.md)，未决与已关闭事项见
[审查 backlog](../review_backlog.yaml)。

<a id="2026-10-18"></a>
| 日期 | 记录与证据 |
| --- | --- |
| 2026-10-19 | [架构评估](2026-10-19/architecture_assessment.md)；[初审发现](2026-10-19/architecture_findings.yaml)；[问题1/2修复验收](2026-10-19/architecture_repairs.md)；[修复证据](2026-10-19/architecture_repair_findings.yaml) |
| 2026-10-18 | [文件整理与验收](2026-10-18/repository_cleanup.md)；[文档体系与 skill 重写](2026-10-18/documentation_rewrite.md) |
| 2026-10-12 | [维护期审查基线与修复验收](2026-10-12/review_record_20261012.md)；[初审发现](2026-10-12/review_findings_20261012.yaml)、[修复发现](2026-10-12/review_fix_findings_20261012.yaml)、[提交检查](2026-10-12/review_commit_findings_20261012.yaml)、[初审快照](2026-10-12/review_evidence_20261012.json)、[修复快照](2026-10-12/review_fix_evidence_20261012.json) |
| 2026-09-16 | [因子审查发现](2026-09-16/review_findings_20260916_factors.yaml) |
| 2026-09-13 | [初次发现](2026-09-13/review_findings_20260913.yaml)、[后续发现](2026-09-13/review_findings_20260913_second.yaml) |
| 2026-08-03 | [计算核验记录](2026-08-03/audit_result_verify_20260803.md) |
| 2026-08-02 | [全功能核查记录](2026-08-02/audit_record_20260802.md) |

2026-10-18 从 `docs/` 根目录迁入，原文件名和历史数值保留。2026-10-12 的
可执行复现脚本在 [scripts/diagnostics](../../scripts/diagnostics/review_evidence_20261012.py)，
从项目根目录运行：

```bash
.venv/bin/python scripts/diagnostics/review_evidence_20261012.py
```

该命令只运行合成数据场景；真实数据核验参数和限制见对应日期记录。
