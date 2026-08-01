"""策略构造层 — YAML 文件加载 与 程序化构建。

两种等价的策略构造路径：

1. YAML 文件 → load_strategy(path)
2. Python dict → build_strategy(cls, config, ...)

两者共享同一套校验与构建逻辑：
  - factor_specs 只引用因子库里的名字（factor: name）；加载时解析为
    {name, weight, ascending}，并把传递引用闭包挂到实例 FACTOR_NODES
    （引擎 preload 据此规划数据供给并把因子物化为列）
  - factor_specs / filter_rules 经 __init__ 传为实例属性（不做类变量 mutation）
  - conditions 在 YAML 中是顶层键，合并进 config
    （程序化路径直接写入 config 即可）

买卖/调仓逻辑不在本层——YAML 里的 strategy 键指向用户自己的 Strategy 子类。"""

import importlib
import logging
from datetime import date, timedelta
from pathlib import Path

import yaml

from btcore.factors.library import load_library, resolve_closure, resolve_spec
from btcore.filters import INDEX_LOOKBACK_DAYS
from btcore.ml.spec import SCOPE_HOLDING, SCOPE_PANEL, parse_models
from btcore.strategy import Strategy

logger = logging.getLogger(__name__)

_KNOWN_FILTER_KEYS = {
    "exclude_st", "exclude_new_stock", "exclude_boards",
    "exclude_industries", "min_price", "exclude_loss",
    "index_universe", "factor_universe",
}

_CONDITION_KEYS = {"stop_loss_pct", "take_profit_pct", "trailing_pct", "model_exit"}


def build_strategy(
    cls: type[Strategy],
    config: dict,
    *,
    factor_specs: list[dict] | None = None,
    filter_rules: dict | None = None,
    factor_library: str | dict | None = None,
    models: dict | None = None,
    strategy_dir: str = "",
) -> Strategy:
    """用 Python dict 构造策略实例（无需 YAML 文件）。

    Args:
        cls: Strategy 子类。
        config: 策略配置（initial_capital / max_positions 及策略自定义键）。
            conditions 直接写入 config 中对应键。
        factor_specs: [{name, weight?, ascending?}]，名称引用 factor_library 里的因子，
            或 models 节 panel 模型的物化列名（ml_<模型名>）。不传时用类默认值。
        filter_rules: {exclude_st?, min_price?, index_universe?, ...}。
        factor_library: 因子库文件路径或预加载的 dict；缺省用 factors/library.yaml。
        models: YAML models 节原始 mapping（{模型名: {artifact, role?, ...}}）。
        strategy_dir: 策略 YAML 所在目录（models artifact 相对路径解析用）。

    Returns:
        完整的 Strategy 实例（含 FACTOR_SPECS / FACTOR_NODES / MODEL_SPECS /
        FILTER_RULES）。
    """
    # 不污染调用方 dict：models_meta 注入与 conditions 校验改写只落在副本上
    config = dict(config)

    # conditions 键校验（YAML 路径与程序化路径统一 fail-fast）
    if config.get("conditions"):
        config["conditions"] = _validate_conditions(config["conditions"])

    # 加载因子库：str → load_library；dict → 直接用；None → 默认路径
    if factor_library is None:
        library = load_library()
    elif isinstance(factor_library, dict):
        library = factor_library
    else:
        library = load_library(factor_library)

    # ML 模型：先于 factor_specs 解析——模型特征是闭包的一部分，
    # panel scope 模型的分数列名（ml_<name>）是 factor_specs 的合法引用
    model_specs = parse_models(models, strategy_dir) if models else []
    panel_columns = {m.column for m in model_specs if m.scope == SCOPE_PANEL}
    holding_columns = {m.column for m in model_specs if m.scope == SCOPE_HOLDING}
    all_columns = panel_columns | holding_columns

    # model_exit 规则引用的模型必须已声明（静默读不到列 = 静默不触发，
    # 属明确声明的依赖缺失，fail-fast）
    declared_models = {m.name for m in model_specs}
    for rule in (config.get("conditions") or {}).get("model_exit") or []:
        if rule["model"] not in declared_models:
            raise ValueError(
                f"conditions.model_exit 引用了未声明的模型 {rule['model']!r}，"
                f"models 节中已声明: {sorted(declared_models)}"
            )
        m = next((s for s in model_specs if s.name == rule["model"]), None)
        if m is not None and m.post_transform != "none":
            logger.warning(
                "conditions.model_exit 引用模型 %s 的 post_transform=%s——"
                "阈值比较作用于变换后分数，持仓数过小时截面 rank/zscore 退化"
                "（单持仓 xs_rank 恒为 1.0，每次都会触发），建议 none",
                m.name, m.post_transform,
            )

    specs = _resolve_factor_specs(factor_specs or [], library, all_columns)
    used_ml = {s["name"] for s in specs if s["name"].startswith("ml_")}
    if used_ml & holding_columns:
        raise ValueError(
            f"factor_specs 引用了 holding scope 模型列 "
            f"{sorted(used_ml & holding_columns)}——其特征依赖账户状态，"
            "不在 preload 物化分数列（决策时点注入持仓 bar），不能参与评分"
        )

    # 模型因子特征并入 specs（materialize_only），进因子闭包统一物化；
    # 未登记的因子名在此 fail-fast
    existing = {s["name"] for s in specs}
    for m in model_specs:
        for fname in m.features:
            if fname not in existing:
                specs.append(
                    resolve_spec({"factor": fname, "materialize_only": True}, library)
                )
                existing.add(fname)

    if specs:
        closure_names = [s["name"] for s in specs if not s["name"].startswith("ml_")]
        strategy_nodes = resolve_closure(closure_names, library) if closure_names else None
    else:
        strategy_nodes = None

    _check_factor_conflicts(specs, cls)

    rules = _validate_filter_rules(filter_rules or {})

    # 模型 raw 特征列并入 REQUIRED_FIELDS（引擎列裁剪据此向 backend 请求）
    raw_features = sorted({f for m in model_specs for f in m.raw_features})

    if model_specs:
        config["models_meta"] = [m.run_summary() for m in model_specs]

    strategy = cls(config=config, factor_specs=specs, filter_rules=rules)
    if strategy_nodes:
        strategy.FACTOR_NODES = strategy_nodes
    if model_specs:
        strategy.MODEL_SPECS = model_specs
        if raw_features:
            strategy.REQUIRED_FIELDS = sorted(
                set(strategy.REQUIRED_FIELDS) | set(raw_features)
            )
        # ML_EXIT 是意图中性的成交机制（带审计标签的次日开盘卖出），
        # 策略可经 ConditionBuilder 的 model_exit 规则自行生成该条件单
        from btcore.ml import conditions as ml_conditions
        ml_conditions.register()
    _attach_index_universe(strategy, rules)
    _attach_factor_universe(strategy, rules)

    return strategy


def load_strategy(path: str) -> Strategy:
    """加载 YAML 策略文件，返回 Strategy 实例。

    本函数是 build_strategy 的 YAML 适配器：解析文件后提取参数并委托给
    build_strategy，两者构造出的策略实例行为完全等价。
    """
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f)
    if not isinstance(doc, dict):
        raise ValueError(f"策略 YAML 必须是 mapping: {path}")

    cls = _resolve_class(doc.get("strategy"), path)
    config = dict(doc.get("config") or {})

    # factor_library 路径相对策略 YAML 所在目录解析；缺省用 factors/library.yaml
    lib_path = doc.get("factor_library")
    if lib_path and not Path(lib_path).is_absolute():
        lib_path = str(Path(path).parent / lib_path)
    factor_library: str | None = lib_path if lib_path else None

    factor_specs = doc.get("factor_specs")
    filter_rules = doc.get("filter_rules")

    # conditions 在 YAML 中是顶层键，合并进 config（键校验在 build_strategy 内统一做）
    conditions = doc.get("conditions")
    if conditions:
        config["conditions"] = conditions

    yaml_dir = str(Path(path).parent)
    return build_strategy(
        cls,
        config,
        factor_specs=factor_specs,
        filter_rules=filter_rules,
        factor_library=factor_library,
        models=doc.get("models"),
        strategy_dir=yaml_dir,
    )


def _attach_index_universe(strategy: Strategy, filter_rules: dict) -> None:
    """filter_rules.index_universe 配置且策略未自定义 get_universe 时，
    生成默认 get_universe：指数成分区间并集，供引擎 preload 裁剪数据量。
    逐日精确成分仍由 StockFilter 在 filter() 里按快照取交集。
    """
    codes = filter_rules.get("index_universe")
    if not codes or type(strategy).get_universe is not Strategy.get_universe:
        return
    codes = list(codes)
    warned = False

    def get_universe(provider, start: str, end: str) -> list[str] | None:
        nonlocal warned
        if not hasattr(provider.backend, "get_index_members"):
            if not warned:
                warned = True
                logger.warning(
                    "index_universe 已开启但 backend 未提供 get_index_members，"
                    "白名单规则不生效"
                )
            return None
        lookback = (
            date.fromisoformat(start) - timedelta(days=INDEX_LOOKBACK_DAYS)
        ).strftime("%Y%m%d")
        snapshots = provider.backend.get_index_members(codes, lookback, end)
        if not snapshots:
            return None
        return sorted(set().union(*snapshots.values()))

    strategy.get_universe = get_universe


def _attach_factor_universe(strategy: Strategy, filter_rules: dict) -> None:
    """filter_rules.factor_universe 配置且策略未自定义 get_factor_universe 时，
    生成默认 get_factor_universe：指数成分区间并集，供引擎 preload 加载因子计算所需数据。
    """
    codes = filter_rules.get("factor_universe")
    if not codes:
        return
    if type(strategy).get_factor_universe is not Strategy.get_factor_universe:
        return
    codes = list(codes)
    warned = False

    def get_factor_universe(provider, start: str, end: str) -> list[str] | None:
        nonlocal warned
        if not hasattr(provider.backend, "get_index_members"):
            if not warned:
                warned = True
                logger.warning(
                    "factor_universe 已配置但 backend 未提供 get_index_members，"
                    "因子计算域不生效，回退为交易域"
                )
            return None
        lookback = (
            date.fromisoformat(start) - timedelta(days=INDEX_LOOKBACK_DAYS)
        ).strftime("%Y%m%d")
        snapshots = provider.backend.get_index_members(codes, lookback, end)
        if not snapshots:
            return None
        return sorted(set().union(*snapshots.values()))

    strategy.get_factor_universe = get_factor_universe


def _resolve_class(spec, path: str) -> type:
    if not spec or not isinstance(spec, str) or ":" not in spec:
        raise ValueError(
            f"策略 YAML 缺少必填键 strategy（格式 module:Class）: {path}"
        )
    module_name, class_name = spec.split(":", 1)
    try:
        module = importlib.import_module(module_name)
        cls = getattr(module, class_name)
    except (ImportError, AttributeError) as exc:
        raise ValueError(f"无法导入策略类 {spec}: {exc}") from exc
    if not (isinstance(cls, type) and issubclass(cls, Strategy)):
        raise ValueError(f"{spec} 不是 btcore.strategy.Strategy 的子类")
    return cls


def _resolve_factor_specs(
    specs: list,
    library: dict,
    ml_columns: frozenset | set = frozenset(),
) -> list[dict]:
    if not isinstance(specs, list):
        raise ValueError("factor_specs 必须是 list")
    resolved = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ValueError(f"factor_specs[{i}] 必须是 mapping: {spec!r}")
        try:
            raw = dict(spec)
            # 程序化路径直接传 name；YAML 路径传 factor。统一转为 factor 键交给 resolve_spec。
            if "name" in raw and "factor" not in raw:
                raw["factor"] = raw.pop("name")
            name = raw.get("factor", "")
            if isinstance(name, str) and name.startswith("ml_"):
                # 模型物化列：不走因子库解析，但必须对应 models 节的 panel 模型
                if name not in ml_columns:
                    raise ValueError(
                        f"引用了未声明的模型列 {name!r}——"
                        "models 节中没有对应的 panel 模型"
                    )
                resolved.append({
                    "name": name,
                    "weight": float(raw.get("weight", 1.0)),
                    "ascending": bool(raw.get("ascending", False)),
                    "materialize_only": bool(raw.get("materialize_only", False)),
                })
                continue
            resolved.append(resolve_spec(raw, library))
        except ValueError as exc:
            raise ValueError(f"factor_specs[{i}]: {exc}") from exc
    return resolved


def _validate_filter_rules(rules: dict) -> dict:
    if not isinstance(rules, dict):
        raise ValueError("filter_rules 必须是 dict")
    for key in rules:
        if key not in _KNOWN_FILTER_KEYS:
            logger.warning("filter_rules 含未知键 %r，StockFilter 将忽略", key)
    return dict(rules)


def _validate_conditions(conditions: dict) -> dict:
    if not isinstance(conditions, dict):
        raise ValueError("conditions 必须是 dict")
    for key, value in conditions.items():
        if key not in _CONDITION_KEYS:
            raise ValueError(
                f"未知 conditions 键 {key!r}，支持: {sorted(_CONDITION_KEYS)}"
            )
        if key == "model_exit":
            # [{model, threshold}]：holding scope 模型分数超阈值时生成 ML_EXIT
            if not isinstance(value, list):
                raise ValueError("conditions.model_exit 必须是 list")
            for i, rule in enumerate(value):
                if not isinstance(rule, dict) or "model" not in rule:
                    raise ValueError(
                        f"conditions.model_exit[{i}] 必须是含 model 键的 mapping"
                    )
                th = rule.get("threshold", 0.5)
                if not isinstance(th, (int, float)) or not 0 < th < 1:
                    raise ValueError(
                        f"conditions.model_exit[{i}].threshold 必须 ∈ (0,1): {th!r}"
                    )
            continue
        if not isinstance(value, (int, float)) or not 0 < value < 1:
            raise ValueError(f"conditions.{key} 必须是 (0,1) 内的数值: {value!r}")
    return dict(conditions)


def _check_factor_conflicts(specs: list[dict], strategy_cls: type) -> None:
    """检查 scoring 因子与 exit 条件引用的因子是否存在冲突。

    三项检查：
    1. 同一因子同时标记为 scoring 和 materialize_only → WARNING
    2. 策略 CONDITION_FACTORS 与 scoring 因子有交集 → WARNING
    3. CONDITION_FACTORS 中的因子未在 factor_specs 中登记 → WARNING
    """
    scoring = {s["name"] for s in specs if not s.get("materialize_only")}
    mat_only = {s["name"] for s in specs if s.get("materialize_only")}

    both = scoring & mat_only
    if both:
        logger.warning(
            "因子交叉冲突: %s 同时标记为评分因子和仅物化因子，"
            "可能是配置错误——请检查 factor_specs",
            sorted(both),
        )

    cond_factors = getattr(strategy_cls, "CONDITION_FACTORS", None)
    if cond_factors is None:
        # 子类覆盖为 None → 等同空集，跳过
        return
    cond_factors = set(cond_factors)

    overlap = scoring & cond_factors
    if overlap:
        logger.warning(
            "entry/exit 因子冲突: %s 同时用于评分和退出条件判断，"
            "买入可能因同一因子回落而被卖出——请检查 factor_specs 和 CONDITION_FACTORS",
            sorted(overlap),
        )

    all_spec_names = {s["name"] for s in specs}
    unregistered = cond_factors - all_spec_names
    if unregistered:
        logger.warning(
            "CONDITION_FACTORS 引用未登记因子: %s 不在 factor_specs 中，"
            "引擎不会物化该列——条件单 handler 读取 bar 时将得到 None",
            sorted(unregistered),
        )
