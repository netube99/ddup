#!/usr/bin/env python3
"""参数扫描：批量运行回测，探索参数空间。

用法:
    python scripts/sweep.py sweep_config.yaml --start 20240101 --end 20240630 \
        --out results/sweep.duckdb

每组参数作为标准 run 写入 --out 库的 runs 表（config_json 含参数），
compare.py / report.py 原生可读；sweep_results 表保留参数标签汇总。
"""

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

from btcore import database
from research.cli_common import latest_run_id
from research.sweep import expand_params, nested_set


def _tail_error(text: str, max_lines: int = 8) -> str:
    """保留 stderr 末尾非空行：真实异常总在 traceback 末尾，截前 200 字符会丢掉它。"""
    lines = [ln for ln in (text or "").rstrip().splitlines() if ln.strip()]
    if not lines:
        return "(无错误输出)"
    return "\n".join(lines[-max_lines:])


def main():
    parser = argparse.ArgumentParser(description="参数扫描回测")
    parser.add_argument("sweep_config", help="sweep 配置文件 YAML")
    parser.add_argument("--start", required=True, help="回测起始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="回测结束日期 YYYYMMDD")
    parser.add_argument("--out", default="sweep_result.duckdb", help="输出数据库")
    parser.add_argument("--capital", type=float, default=None, help="初始资金（覆盖 YAML config）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印参数组合，不运行")
    args = parser.parse_args()

    try:
        with open(args.sweep_config) as f:
            config = yaml.safe_load(f)
    except yaml.YAMLError as e:
        parser.error(f"sweep 配置 YAML 非法: {e}")

    if not isinstance(config, dict) or not isinstance(config.get("base"), str):
        parser.error(
            f"sweep 配置顶层必须是含 base 键的 mapping: {args.sweep_config}"
        )
    base_path = config["base"]
    params_def = config.get("params")

    # base 是扫描的必需输入：缺失时 fail-fast（--dry-run 也预检），
    # 避免逐组重复 FileNotFoundError traceback
    if not Path(base_path).exists():
        print(f"错误: base 策略配置不存在: {base_path}", file=sys.stderr)
        return 1

    if not isinstance(params_def, dict) or not params_def:
        parser.error("sweep 配置的 params 为空：没有可扫描的参数组合")

    combinations = expand_params(params_def)
    print(f"参数组合数: {len(combinations)}")

    if args.dry_run:
        for label, params in combinations:
            print(f"  {label}")
        return 0

    # 准备输出数据库；已存在的文件先做旧格式拒止（SQLite 会被
    # sqlite_scanner 静默打开），再启动子进程
    out_path = Path(args.out)
    if out_path.exists():
        database.assert_duckdb_file(str(out_path), "结果库")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        n_failed = 0
        for i, (label, params) in enumerate(combinations):
            print(f"\n[{i+1}/{len(combinations)}] {label}")

            # 生成临时 config
            with open(base_path) as f:
                base_config = yaml.safe_load(f)

            for key_path, value in params.items():
                nested_set(base_config, key_path, value)

            # factor_library 相对基 YAML 目录解析（load_strategy 语义）；临时
            # config 写在 tmpdir，相对路径会解析到错误位置——改写为绝对路径
            lib = base_config.get("factor_library")
            if lib and not Path(lib).is_absolute():
                base_config["factor_library"] = str(
                    (Path(base_path).resolve().parent / lib).resolve()
                )

            tmp_config = tmpdir / f"config_{i}.yaml"
            with open(tmp_config, "w") as f:
                yaml.dump(base_config, f, allow_unicode=True)

            # 运行回测：每组参数作为标准 run 写入同一输出库（runs 表，
            # compare.py/report.py 原生可读）；同时生成 HTML 报告纯属浪费
            cmd = [
                sys.executable, "scripts/run.py",
                str(tmp_config),
                "--start", args.start,
                "--end", args.end,
                "--out", str(out_path),
                "--no-report",
            ]
            if args.capital is not None:
                cmd.extend(["--capital", str(args.capital)])
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                # 保留 stderr 末尾真实异常（截前 200 字符会把 ValueError 切掉）
                detail = _tail_error(result.stderr or result.stdout)
                print(f"  FAIL:\n{detail}")
                n_failed += 1
                continue

            # 聚合结果：读取刚写入的 run 的 stats_json，附加参数标签
            try:
                row = None
                out_conn = database.connect_result_db(str(out_path), read_only=True)
                try:
                    rid = latest_run_id(out_conn)
                    if rid is not None:
                        row = out_conn.execute(
                            "SELECT stats_json FROM runs WHERE run_id = ?", [rid]
                        ).fetchone()
                finally:
                    out_conn.close()

                if row and row[0]:
                    stats = json.loads(row[0])
                    stats["label"] = label
                    stats["params"] = {str(k): v for k, v in params.items()}

                    # 汇总表（兼容旧 CLI 输出；归因/对比请直接用 runs 表）
                    out_conn = database.connect_result_db(str(out_path))
                    try:
                        out_conn.execute(
                            "CREATE SEQUENCE IF NOT EXISTS sweep_results_id_seq;"
                            " CREATE TABLE IF NOT EXISTS sweep_results ("
                            "id BIGINT PRIMARY KEY DEFAULT nextval('sweep_results_id_seq'), "
                            "label VARCHAR, params_json VARCHAR, stats_json VARCHAR)"
                        )
                        out_conn.execute(
                            "INSERT INTO sweep_results (label, params_json, stats_json) "
                            "VALUES (?, ?, ?)",
                            (label, json.dumps(params, default=str),
                             json.dumps(stats, default=str)),
                        )
                        out_conn.commit()
                    finally:
                        out_conn.close()

                    total_return = stats.get("total_return", 0)
                    sharpe = stats.get("sharpe", 0)
                    mdd = stats.get("max_drawdown", 0)
                    print(f"  OK: return={total_return:.1%} sharpe={sharpe:.2f} mdd={mdd:.1%}")
            except Exception as e:
                print(f"  ERROR aggregating: {e}")

    # 输出汇总：全失败不得打印成功字样，且必须非零退出
    if n_failed == len(combinations):
        print("\n所有参数组合均失败，未产出有效结果（检查上方 FAIL）", file=sys.stderr)
        return 1
    if not out_path.exists():
        print("所有参数组合均未产出结果库，跳过汇总（检查上方 FAIL/ERROR）",
              file=sys.stderr)
        return 1
    print(f"\n结果已保存到: {out_path}")
    out_conn = database.connect_result_db(str(out_path), read_only=True)
    try:
        rows = out_conn.execute(
            "SELECT label, stats_json FROM sweep_results ORDER BY id"
        ).fetchall()
    finally:
        out_conn.close()
    if rows:
        print(f"\n{'参数组合':<50} {'收益':>8} {'Sharpe':>7} {'MDD':>8}")
        print("-" * 80)
        for label, stats_json in rows:
            stats = json.loads(stats_json)
            total_return = stats.get("total_return", 0)
            sharpe = stats.get("sharpe", 0)
            mdd = stats.get("max_drawdown", 0)
            print(f"{label:<50} {total_return:>7.1%} {sharpe:>7.2f} {mdd:>7.1%}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
