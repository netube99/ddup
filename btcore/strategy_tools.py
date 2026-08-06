"""策略编写工具 — 用户 select() / calc_conditions() 的可选机制。

只是机制，不含任何买卖决策：截面数据整理、按 FACTOR_SPECS 求值并合成
得分、声明式条件单构建。排几名、买几只、何时调仓，
由用户策略自己决定。
"""

import logging

import pandas as pd

from btcore.types import bar_get

logger = logging.getLogger(__name__)

# conditions 声明式规则顶层键（与 strategy_loader._CONDITION_KEYS 一致；
# loader 路径先行校验，这里覆盖 cls(config=...) 直接构造路径——EDGE-09）。
# 校验只查顶层规则键，不校验条件单 dict 的 type 名（type 由
# register_condition_handler 注册集 + validate_condition_types 管辖，
# 自定义 handler 类型不受影响）。
_CONDITION_KEYS = frozenset({
    "stop_loss_pct", "take_profit_pct", "trailing_pct", "model_exit",
})


def bars_to_df(bars: dict) -> pd.DataFrame:
    """当日截面 dict-of-dicts → symbol 索引的 DataFrame，供 eval_factor_specs 使用。"""
    if not bars:
        return pd.DataFrame()
    return pd.DataFrame.from_dict(bars, orient="index")


def eval_factor_specs(
    df: pd.DataFrame,
    factor_specs: list[dict],
) -> tuple[pd.DataFrame, pd.Series]:
    """按 FACTOR_SPECS 读物化因子列并合成加权得分。

    每条 spec: {name, weight=1.0, ascending=False, materialize_only=False}；
    因子值由引擎在 preload 时物化为 df 的列（找不到列说明 FACTOR_NODES 未挂接）。
    materialize_only=True 的条目仅写入 factor_df 供 calc_conditions 读取判断，
    不参与百分比排名和加权合成。
    其余因子先转截面 percentile rank（ascending=True 时值小者得分高），
    再按 weight 加权平均为 score（∈ [0,1]，越大越优）。

    Returns:
        (factor_df, score): factor_df 每列一个因子值；score 为合成得分 Series，
        索引均为 symbol。factor_specs 为空时 score 为全 1.0。
    """
    factor_df = pd.DataFrame(index=df.index)
    score = pd.Series(0.0, index=df.index)
    total_weight = 0.0

    for spec in factor_specs or []:
        name = spec["name"]
        # DUP-08a: 列校验提到循环顶部，materialize_only 与评分分支共用
        if name not in df.columns:
            raise ValueError(
                f"因子列 {name!r} 不在截面数据里——引擎未物化"
                "（策略缺少 FACTOR_NODES？请经 strategy_loader 加载）"
            )
        if spec.get("materialize_only"):
            factor_df[name] = df[name]
            continue
        values = df[name]
        factor_df[name] = values
        weight = float(spec.get("weight", 1.0))
        pct_rank = values.rank(pct=True, ascending=not spec.get("ascending", False))
        score = score + pct_rank.fillna(0.0) * weight
        total_weight += weight

    if total_weight > 0:
        score = score / total_weight
    else:
        score = pd.Series(1.0, index=df.index)
    return factor_df, score


class ConditionBuilder:
    """由声明式规则生成条件单，并跟踪移动止盈所需的最高价状态。

    策略层工具：把 YAML 里声明的 conditions 规则翻译成引擎的条件单 dict。
    用户策略在 calc_conditions 里委托给本类；规则全空时返回空列表（不使用条件单）。

    支持的规则（值均为比例，∈ (0,1)）：
      stop_loss_pct   → STOP_LOSS    价格 = 成本价 * (1 - pct)
      take_profit_pct → TAKE_PROFIT  价格 = 成本价 * (1 + pct)
      trailing_pct    → TRAILING_TP  价格 = 持仓期间最高收盘价 * (1 - pct)，
                        最高收盘价由本类逐日跟踪（触发仍在盘中 low）
      model_exit      → ML_EXIT      [{model, threshold}]：bar 中 ml_<model> 分数
                          >= threshold 时触发（分数由引擎物化/注入，含义由策略定义）
    """

    def __init__(self, rules: dict):
        rules = rules or {}
        self._validate_rules(rules)
        self._rules = rules
        self._high: dict[str, float] = {}

    @staticmethod
    def _validate_rules(rules: dict) -> None:
        """条件声明顶层键/取值校验（与 strategy_loader._validate_conditions
        同口径，覆盖程序化直接构造路径——EDGE-09）。"""
        if not isinstance(rules, dict):
            raise ValueError("conditions 必须是 dict")
        for key, value in rules.items():
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
                raise ValueError(
                    f"conditions.{key} 必须是 (0,1) 内的数值: {value!r}"
                )

    def calc(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """与 Strategy.calc_conditions 同签名，返回条件单 dict 列表。"""
        rules = self._rules
        if not rules:
            return []

        conds = []
        if "stop_loss_pct" in rules:
            conds.append({
                "type": "STOP_LOSS",
                "price": entry_price * (1 - rules["stop_loss_pct"]),
            })
        if "take_profit_pct" in rules:
            conds.append({
                "type": "TAKE_PROFIT",
                "price": entry_price * (1 + rules["take_profit_pct"]),
            })
        if "trailing_pct" in rules:
            close = bar_get(bar, "close", entry_price) or entry_price
            high = max(self._high.get(symbol, entry_price), close)
            self._high[symbol] = high
            conds.append({
                "type": "TRAILING_TP",
                "price": high * (1 - rules["trailing_pct"]),
            })
        for rule in rules.get("model_exit") or []:
            model = rule["model"]
            threshold = float(rule.get("threshold", 0.5))
            score = bar_get(bar, f"ml_{model}")
            if score is not None and score >= threshold:
                conds.append({
                    "type": "ML_EXIT",
                    "model": model,
                    "score": round(float(score), 4),
                })
        return conds

    def rescale(self, symbol: str, scale: float) -> None:
        """除权除息后同步缩放 trailing 锚点（引擎 corporate.adjust 后调用）。

        锚点 _high 是裸收盘价口径，除权日价格台阶式下降；不 rescale 会让
        TRAILING_TP 触发价保留除权前高点，次日 calc 重算即误触发（2026-08
        实证 300501.SZ 10送4.6+派0.6 后开盘误卖）。
        """
        if symbol in self._high:
            self._high[symbol] *= scale

    def prune(self, live_symbols) -> None:
        """清理已平仓标的的 trailing 状态。

        简便用法：在 select() 里以当前持仓调用本方法（基于持仓 diff）。
        精确用法：策略实现 on_fills hook，按 trigger 感知条件单平仓时点与价格。
        """
        live = set(live_symbols)
        for symbol in list(self._high):
            if symbol not in live:
                del self._high[symbol]



