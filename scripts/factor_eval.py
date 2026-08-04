"""因子评估 CLI — IC 分析 / 分层回测 / 因子相关性矩阵（薄壳）。

用法:
    python scripts/factor_eval.py mom20,vol_z,ep_z \
        --start 20240101 --end 20240630 [--universe CSI300] [--forward 5] \
        [--n-quantiles 5]

IC 衰减模式（多前瞻期）:
    python scripts/factor_eval.py cci_z,turnover_z \
        --start 20240101 --end 20240630 --decay 1,3,5,10,20

评估编排 run_eval 在 research/factor_eval.py（测试直接 import 该模块）。
行情数据库由 adapters/tushare.py 的 _DEFAULT_DB_PATH 决定。
口径与引擎同源：因子 preload 前伸 warmup 窗口（fplan main_days），
坍缩因子（市场广度）走全市场流式 compute_breadth，--universe 按
point-in-time 成分过滤（与 ml_train 训练域一致）。
"""

import argparse
import sys

from research import cli_common
from research.factor_eval import run_eval


def main() -> int:
    parser = argparse.ArgumentParser(
        description="因子评估 — IC / 分层回测 / 相关性矩阵",
    )
    parser.add_argument(
        "factors", nargs="?", default="",
        help="逗号分隔的因子名称（来自 factors/library.yaml）；--model 时可省略",
    )
    parser.add_argument(
        "--model", default=None,
        help="ML 模型 ONNX 路径（meta 为同名 .meta.json）——"
             "模型分数物化为 ml_<name> 列后与因子同口径评估",
    )
    parser.add_argument("--start", required=True, help="开始日期 YYYYMMDD")
    parser.add_argument("--end", required=True, help="结束日期 YYYYMMDD")
    parser.add_argument(
        "--universe", default=None,
        help="指数代码或简称（CSI300/CSI500/CSI1000），默认全市场",
    )
    parser.add_argument(
        "--forward", type=int, default=5,
        help="前瞻收益天数（默认 5，即 1 周）",
    )
    parser.add_argument(
        "--decay", type=str, default=None,
        help="多前瞻期 IC 衰减模式（逗号分隔天数，如 1,3,5,10,20）",
    )
    parser.add_argument(
        "--n-quantiles", type=int, default=5,
        help="分层回测档数（默认 5）",
    )
    parser.add_argument(
        "--benchmark", default=None,
        help="基准指数代码（因子引用 idx_ret 时必需，口径同引擎）",
    )
    args = parser.parse_args()

    # --decay 与 --forward 互斥
    if args.decay and args.forward != 5:
        print("错误：--decay 与 --forward 不能同时指定", file=sys.stderr)
        return 1

    factor_names = [n.strip() for n in args.factors.split(",") if n.strip()]
    provider = cli_common.make_provider()
    try:
        return run_eval(
            provider.backend,
            factor_names,
            args.start,
            args.end,
            model_path=args.model,
            universe=args.universe,
            forward=args.forward,
            decay=args.decay,
            n_quantiles=args.n_quantiles,
            benchmark=args.benchmark,
        )
    except ValueError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1
    finally:
        provider.backend.close()


if __name__ == "__main__":
    sys.exit(main())
