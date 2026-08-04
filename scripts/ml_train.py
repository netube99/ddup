"""ML 模型训练 CLI — 与引擎同一物化路径，产出 ONNX + meta v3。

用法:
    # panel scope（无账户态特征）：标签 = horizon 日前向收益的截面排名
    .venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
        --model alpha_xs --start 20220101 --end 20250630 --horizon 5

    # holding scope（features 含 state）：标签 = 回测 trade_log 的
    # TREND_BREAK + 净亏损，训练持仓预警模型
    .venv/bin/python scripts/ml_train.py strategies/my_strategy/config.yaml \
        --model tb_guard --start 20220101 --end 20250630 \
        --db results/baseline.db --lookahead 3

scope 由 state_features 自动推导，与引擎一致。模型的意图（分数如何
消费、阈值多少）不在训练侧——由策略 YAML / 代码自行定义。

特征契约：策略 YAML models.<name> 的 meta（已训练过）或内联 features
（首次训练引导）：
    models:
      alpha_xs:
        artifact: ml_model/alpha_xs.onnx
        features:
          factors: [roc_5, cci_z, close_vs_ma20]
          raw: [turnover_rate, pe_ttm]
          # state: [hold_days, ret_from_entry]   # holding scope

产出物写到 artifact 声明的路径（相对策略目录）：
    <name>.onnx        XGBoost ONNX 模型
    <name>.meta.json   特征契约 + scaler + 评估指标（version=3）
    # 后变换用 --post-transform 指定（YAML 里写 post_transform 无效，
    # 训练/推理统一以 meta 为准）
"""

import argparse
import logging
import sys
from pathlib import Path

import yaml

from btcore.factors.library import load_library
from btcore.filters import resolve_index_snapshots
from btcore.ml import dataset, labels
from btcore.ml.export import export_model
from btcore.ml.spec import SCOPE_HOLDING, ModelSpec
from btcore.ml.trainer import train_guard, train_panel
from research import cli_common

logger = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="ML 模型训练 — 与引擎同一物化路径")
    p.add_argument("yaml", help="策略 YAML 配置文件路径")
    p.add_argument("--model", required=True, help="models 节中的模型名")
    p.add_argument("--start", required=True, help="训练起始日期 YYYYMMDD")
    p.add_argument("--end", required=True, help="训练结束日期 YYYYMMDD")
    p.add_argument("--horizon", type=int, default=5,
                   help="panel 标签前瞻天数（默认 5）")
    p.add_argument("--db", help="holding scope 标签来源：含 trade_log 的结果库")
    p.add_argument("--run-id", type=int, default=None,
                   help="标签取用的 run_id（缺省取最新 completed run）")
    p.add_argument("--lookahead", type=int, default=3,
                   help="holding scope 标签前瞻天数（默认 3）")
    p.add_argument("--post-transform", choices=["none", "xs_rank", "xs_zscore"],
                   help="覆盖分数的截面后变换（缺省读 YAML/meta）")
    p.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
    )

    yaml_path = Path(args.yaml)
    yaml_dir = str(yaml_path.parent)
    with open(yaml_path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)

    models_raw = doc.get("models") or {}
    if args.model not in models_raw:
        print(f"错误: 策略 YAML 的 models 节中没有 {args.model!r}")
        return 1

    # 训练引导：meta 缺失时允许从 YAML 内联 features 构造
    try:
        spec = ModelSpec.from_dict(
            args.model, models_raw[args.model], yaml_dir, require_meta=False,
        )
    except ValueError as e:
        print(f"错误: {e}")
        return 1
    if args.post_transform:
        spec.post_transform = args.post_transform

    is_holding = spec.scope == SCOPE_HOLDING
    if is_holding and not args.db:
        print("错误: holding scope 模型训练需要 --db（trade_log 标签来源）")
        return 1

    lib_path = doc.get("factor_library")
    if lib_path and not Path(lib_path).is_absolute():
        lib_path = str(yaml_path.parent / lib_path)
    library = load_library(lib_path)
    missing = [n for n in spec.features if n not in library]
    if missing:
        print(f"错误: 因子库中缺少特征因子: {missing}")
        return 1

    try:
        backend = cli_common.make_provider().backend
    except ImportError:
        print("错误: 无法导入 adapters.tushare.TushareBackend")
        return 1
    print(f"后端: {type(backend).__name__}")

    # 训练域：index_universe 成分并集，未配置则全市场；
    # 面板构建后再按 point-in-time 成分过滤（训练域 = 引擎逐日计算域）
    symbols = None
    pit_members = None
    index_codes = (doc.get("filter_rules") or {}).get("index_universe", [])
    if index_codes:
        snaps = resolve_index_snapshots(backend, index_codes, args.start, args.end)
        if snaps:
            symbols = sorted(set().union(*snaps.values()))
            pit_members = snaps
            print(f"训练域: {len(symbols)} 只（index_universe 并集，PIT 过滤）")

    benchmark = (doc.get("config") or {}).get("benchmark")

    try:
        if is_holding:
            return _train_holding(
                args, spec, backend, symbols, library, benchmark, pit_members,
            )
        return _train_panel(
            args, spec, backend, symbols, library, benchmark, pit_members,
        )
    except (ValueError, RuntimeError) as e:
        print(f"错误: {e}")
        return 1


def _train_panel(
    args, spec, backend, symbols, library, benchmark, pit_members=None,
) -> int:
    print(f"panel 模型: {len(spec.features)} 因子 + {len(spec.raw_features)} raw, "
          f"horizon={args.horizon}, post_transform={spec.post_transform}")
    panel = dataset.build_panel(
        backend, symbols, args.start, args.end, spec, library, benchmark,
    )
    panel = dataset.apply_pit_membership(panel, pit_members)
    label_df = labels.xs_forward_return(panel, args.horizon)
    result = train_panel(panel, spec.feature_order, label_df, args.horizon)
    print(f"样本: train={result.n_train} test={result.n_test}")
    m = result.metrics
    print(f"IC: mean={m['ic_mean']:.4f} icir={m['icir']:.3f} "
          f"pos_ratio={m['ic_pos_ratio']:.2%} days={m['n_days']}")
    layered = m.get("layered", {})
    print(f"分层: 多空={layered.get('long_short', 0):.4f} "
          f"单调={layered.get('monotonic')}")

    verify = panel[spec.feature_order].astype(float).to_numpy()[:32]
    onnx_path, meta_path = export_model(
        result, spec, spec.artifact,
        label={"type": "xs_fwdret", "horizon": args.horizon},
        train_window=[args.start, args.end],
        verify_rows=verify,
    )
    print(f"导出: {onnx_path}\n      {meta_path}")
    return 0


def _train_holding(
    args, spec, backend, symbols, library, benchmark, pit_members=None,
) -> int:
    pairs_df = labels.extract_trade_pairs(args.db, run_id=args.run_id)
    if pairs_df.empty:
        print("错误: trade_log 中没有可配对的交易回合")
        return 1
    tb_loss = (pairs_df["trigger"] == "TREND_BREAK") & (pairs_df["pnl"] < 0)
    print(f"交易回合: {len(pairs_df)} 条, TB+亏损: {tb_loss.sum()}")

    trade_symbols = sorted(pairs_df["symbol"].unique())
    if symbols is not None:
        trade_symbols = sorted(set(trade_symbols) & set(symbols)) or trade_symbols

    print(f"holding scope 模型: {len(spec.features)} 因子 + "
          f"{len(spec.raw_features)} raw + {len(spec.state_features)} 账户态, "
          f"lookahead={args.lookahead}")
    panel = dataset.build_panel(
        backend, trade_symbols, args.start, args.end, spec, library, benchmark,
    )
    panel = dataset.apply_pit_membership(panel, pit_members)
    samples = labels.build_guard_samples(panel, pairs_df, spec, args.lookahead)
    if samples.empty:
        print("错误: 样本构建为空")
        return 1
    print(f"样本: {len(samples)}, positive={int(samples['label'].sum())} "
          f"({samples['label'].mean():.2%})")

    result = train_guard(samples, spec.feature_order, args.lookahead)
    print(f"train={result.n_train} test={result.n_test}, "
          f"AUC={result.metrics['auc']:.3f} "
          f"precision={result.metrics['precision@0.5']:.3f} "
          f"recall={result.metrics['recall@0.5']:.3f}")

    verify = samples[spec.feature_order].astype(float).to_numpy()[:32]
    onnx_path, meta_path = export_model(
        result, spec, spec.artifact,
        label={"type": "trend_break", "lookahead": args.lookahead},
        train_window=[args.start, args.end],
        verify_rows=verify,
    )
    print(f"导出: {onnx_path}\n      {meta_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
