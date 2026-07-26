"""多 run 回测结果对比 CLI。

用法:
    python scripts/compare.py result.db [--runs 1,2,3] [--html compare.html]

终端打印关键指标对比表；--html 时同时产出对比报告
（指标表 + 归一化净值叠加曲线）。
"""

import argparse
import sys

from research.report import build_compare_table, generate_compare_report, load_runs


def _print_table(header: list[str], rows: list[list[str]]):
    widths = [len(h) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    line = "  ".join(h.ljust(widths[i]) for i, h in enumerate(header))
    print(line)
    print("-" * len(line))
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare multiple runs in a result DB")
    parser.add_argument("db", help="回测结果库路径")
    parser.add_argument("--runs", default=None, help="逗号分隔的 run_id 列表（缺省全部）")
    parser.add_argument("--html", default=None, help="对比 HTML 报告输出路径")
    args = parser.parse_args()

    run_ids = [int(x) for x in args.runs.split(",")] if args.runs else None
    runs = load_runs(args.db, run_ids)
    if len(runs) < 2:
        print("对比至少需要 2 个 run", file=sys.stderr)
        return 1

    header, rows = build_compare_table(runs)
    _print_table(header, rows)

    if args.html:
        generate_compare_report(args.db, args.html, run_ids)
        print(f"html: {args.html}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
