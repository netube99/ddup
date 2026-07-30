#!/usr/bin/env python3
"""参数扫描：批量运行回测，探索参数空间。

用法:
    python scripts/sweep.py sweep_config.yaml --start 20240101 --end 20240630 --out sweep_result.db
"""

import argparse
import itertools
import json
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml


def nested_set(d, key_path, value):
    """按 '.' 分割的路径设置嵌套 dict 值。"""
    keys = key_path.split(".")
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def expand_params(params_def):
    """展开参数空间为笛卡尔积，返回 (param_label, param_dict) 列表。"""
    keys = list(params_def.keys())
    values = [params_def[k] for k in keys]
    results = []
    for combo in itertools.product(*values):
        param_dict = dict(zip(keys, combo))
        label_parts = []
        for k, v in param_dict.items():
            short_k = k.split(".")[-1]  # 取路径最后一段作为简称
            if isinstance(v, float):
                label_parts.append(f"{short_k}={v:.2f}")
            else:
                label_parts.append(f"{short_k}={v}")
        label = ", ".join(label_parts)
        results.append((label, param_dict))
    return results


def main():
    parser = argparse.ArgumentParser(description="参数扫描回测")
    parser.add_argument("sweep_config", help="sweep 配置文件 YAML")
    parser.add_argument("--start", required=True, help="回测起始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="回测结束日期 YYYYMMDD")
    parser.add_argument("--out", default="sweep_result.db", help="输出数据库")
    parser.add_argument("--capital", type=float, default=None, help="初始资金（覆盖 YAML config）")
    parser.add_argument("--dry-run", action="store_true", help="仅打印参数组合，不运行")
    args = parser.parse_args()

    with open(args.sweep_config) as f:
        config = yaml.safe_load(f)

    base_path = config["base"]
    params_def = config["params"]

    combinations = expand_params(params_def)
    print(f"参数组合数: {len(combinations)}")

    if args.dry_run:
        for label, params in combinations:
            print(f"  {label}")
        return

    # 准备输出数据库
    out_path = Path(args.out)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)

        for i, (label, params) in enumerate(combinations):
            print(f"\n[{i+1}/{len(combinations)}] {label}")

            # 生成临时 config
            with open(base_path) as f:
                base_config = yaml.safe_load(f)

            for key_path, value in params.items():
                nested_set(base_config, key_path, value)

            tmp_config = tmpdir / f"config_{i}.yaml"
            with open(tmp_config, "w") as f:
                yaml.dump(base_config, f, allow_unicode=True)

            # 运行回测
            tmp_db = tmpdir / f"result_{i}.db"
            cmd = [
                sys.executable, "scripts/run.py",
                str(tmp_config),
                "--start", args.start,
                "--end", args.end,
                "--out", str(tmp_db),
            ]
            if args.capital is not None:
                cmd.extend(["--capital", str(args.capital)])
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"  FAIL: {result.stderr[:200]}")
                continue

            # 聚合结果：读取 stats_json，附加参数标签
            try:
                tmp_conn = sqlite3.connect(str(tmp_db))
                stats_json = tmp_conn.execute(
                    "SELECT stats_json FROM runs WHERE run_id = 1"
                ).fetchone()
                tmp_conn.close()

                if stats_json:
                    stats = json.loads(stats_json[0])
                    stats["label"] = label
                    stats["params"] = {str(k): v for k, v in params.items()}

                    # 写入输出数据库
                    out_conn = sqlite3.connect(str(out_path))
                    out_conn.execute(
                        "CREATE TABLE IF NOT EXISTS sweep_results ("
                        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                        "label TEXT, params_json TEXT, stats_json TEXT)"
                    )
                    out_conn.execute(
                        "INSERT INTO sweep_results (label, params_json, stats_json) "
                        "VALUES (?, ?, ?)",
                        (label, json.dumps(params, default=str),
                         json.dumps(stats, default=str)),
                    )
                    out_conn.commit()
                    out_conn.close()

                    total_return = stats.get("total_return", 0)
                    sharpe = stats.get("sharpe", 0)
                    mdd = stats.get("max_drawdown", 0)
                    print(f"  OK: return={total_return:.1%} sharpe={sharpe:.2f} mdd={mdd:.1%}")
            except Exception as e:
                print(f"  ERROR aggregating: {e}")

    # 输出汇总
    print(f"\n结果已保存到: {out_path}")
    out_conn = sqlite3.connect(str(out_path))
    rows = out_conn.execute(
        "SELECT label, stats_json FROM sweep_results ORDER BY id"
    ).fetchall()
    if rows:
        print(f"\n{'参数组合':<50} {'收益':>8} {'Sharpe':>7} {'MDD':>8}")
        print("-" * 80)
        for label, stats_json in rows:
            stats = json.loads(stats_json)
            total_return = stats.get("total_return", 0)
            sharpe = stats.get("sharpe", 0)
            mdd = stats.get("max_drawdown", 0)
            print(f"{label:<50} {total_return:>7.1%} {sharpe:>7.2f} {mdd:>7.1%}")
    out_conn.close()


if __name__ == "__main__":
    main()
