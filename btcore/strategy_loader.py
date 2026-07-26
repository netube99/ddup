"""YAML 策略加载器。

把声明式 YAML 翻译成 Strategy 子类实例：
  - factor_specs 只引用因子库里的名字（factor: name）；加载时解析为
    {name, weight, ascending}，并把传递引用闭包挂到实例 FACTOR_NODES
    （引擎 preload 据此规划数据供给并把因子物化为列）
  - factor_specs / filter_rules 经 __init__ 传为实例属性（不做类变量 mutation）
  - conditions 合并进 config["conditions"]，由策略侧 ConditionBuilder 消费
  - risk_rules 合并进 config["risk_rules"]，由引擎强制执行（组合级风控）
  - schedule 声明调仓频率（daily|weekly|monthly），由 strategy_tools 包装 select

买卖/调仓逻辑不在本层——YAML 里的 strategy 键指向用户自己的 Strategy 子类。
"""

import importlib
import logging
from datetime import date, timedelta
from pathlib import Path

import yaml

from btcore.factors.library import load_library, resolve_closure, resolve_spec
from btcore.risk import validate_risk_rules
from btcore.strategy import Strategy
from btcore.strategy_tools import parse_schedule, wrap_strategy

logger = logging.getLogger(__name__)

_KNOWN_FILTER_KEYS = {
    "exclude_st", "exclude_new_stock", "exclude_boards",
    "exclude_industries", "min_price", "exclude_loss",
    "index_universe",
}

_CONDITION_KEYS = {"stop_loss_pct", "take_profit_pct", "trailing_pct"}


def load_strategy(path: str) -> Strategy:
    """加载 YAML 策略文件，返回 Strategy 实例。"""
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
    library = load_library(lib_path)

    factor_specs = _resolve_factor_specs(doc.get("factor_specs") or [], library)
    if factor_specs:
        # 因子闭包挂到实例：引擎 preload 据此规划两路供给并物化因子列
        strategy_nodes = resolve_closure(
            [s["name"] for s in factor_specs], library
        )
    else:
        strategy_nodes = None
    filter_rules = _validate_filter_rules(doc.get("filter_rules") or {})

    conditions = doc.get("conditions")
    if conditions:
        config["conditions"] = _validate_conditions(conditions)

    risk_rules = doc.get("risk_rules")
    if risk_rules:
        config["risk_rules"] = validate_risk_rules(risk_rules)

    strategy = cls(config=config, factor_specs=factor_specs,
                   filter_rules=filter_rules)
    if strategy_nodes:
        strategy.FACTOR_NODES = strategy_nodes
    _attach_index_universe(strategy, filter_rules)

    schedule = doc.get("schedule")
    if schedule is not None:
        strategy = wrap_strategy(strategy, parse_schedule(schedule))

    return strategy


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
            date.fromisoformat(start) - timedelta(days=45)
        ).strftime("%Y%m%d")
        snapshots = provider.backend.get_index_members(codes, lookback, end)
        if not snapshots:
            return None
        return sorted(set().union(*snapshots.values()))

    strategy.get_universe = get_universe


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


def _resolve_factor_specs(specs: list, library: dict) -> list[dict]:
    if not isinstance(specs, list):
        raise ValueError("factor_specs 必须是 list")
    resolved = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            raise ValueError(f"factor_specs[{i}] 必须是 mapping: {spec!r}")
        try:
            resolved.append(resolve_spec(dict(spec), library))
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
        if not isinstance(value, (int, float)) or not 0 < value < 1:
            raise ValueError(f"conditions.{key} 必须是 (0,1) 内的数值: {value!r}")
    return dict(conditions)
