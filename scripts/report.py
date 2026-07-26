"""从回测结果库离线生成单 run HTML 报告。

用法:
    python scripts/report.py result.db [--run-id N] --out report.html

--run-id 缺省取最新 run；老 run 无 stats_json 时现场重算统计指标。
"""

import argparse
import sys

from research.report import generate_report_from_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate single-run HTML report from result DB")
    parser.add_argument("db", help="回测结果库路径")
    parser.add_argument("--run-id", type=int, default=None, help="run_id（缺省取最新）")
    parser.add_argument("--out", required=True, help="HTML 报告输出路径")
    args = parser.parse_args()

    generate_report_from_db(args.db, args.out, run_id=args.run_id)
    print(f"report: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
