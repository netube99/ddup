"""组合级风控机制 — 声明式规则 + 引擎强制执行的纯函数件。

只提供机制：规则校验、回撤熔断状态、买侧裁剪。阈值由策略 YAML 的
risk_rules 声明（loader 并入 config["risk_rules"]），引擎在 select 之后
套用。卖侧永不干预；本模块不依赖 engine / match / database / provider。

规则（均可选、可组合）：
  max_drawdown     总权益自峰值回撤 ≥ 该值触发熔断
  cooldown_days    熔断冷却 N 个交易日（强制只卖不买），缺省 1
  max_position_pct 单票买入金额/目标市值 ≤ 总资产 × 该值
  max_industry_pct 单行业总市值（持仓+新买单）≤ 总资产 × 该值

行业上限是"入场闸"不是"持续配平器"：只在买入时点把关声明金额，
持仓因上涨自然超限不强制减仓；卖出释放的行业余量不在当日回补计算。
"""

import logging

logger = logging.getLogger(__name__)

_PCT_KEYS = {"max_drawdown", "max_position_pct", "max_industry_pct"}
_KNOWN_KEYS = _PCT_KEYS | {"cooldown_days"}


def validate_risk_rules(raw) -> dict:
    """校验并规范化 risk_rules；未知键/非法值立即 ValueError。"""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("risk_rules 必须是 dict")
    for key, value in raw.items():
        if key not in _KNOWN_KEYS:
            raise ValueError(
                f"未知 risk_rules 键 {key!r}，支持: {sorted(_KNOWN_KEYS)}"
            )
        if key in _PCT_KEYS:
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not 0 < value < 1):
                raise ValueError(
                    f"risk_rules.{key} 必须是 (0,1) 内的数值: {value!r}"
                )
        elif (not isinstance(value, int) or isinstance(value, bool)
              or value <= 0):
            raise ValueError(
                f"risk_rules.cooldown_days 必须是正整数: {value!r}"
            )
    rules = dict(raw)
    if "cooldown_days" in rules and "max_drawdown" not in rules:
        raise ValueError("cooldown_days 需配合 max_drawdown 使用")
    if "max_drawdown" in rules:
        rules.setdefault("cooldown_days", 1)
    return rules


class DrawdownBreaker:
    """回撤熔断状态机：峰值回撤 ≥ max_drawdown 触发，冷却 N 个交易日。

    引擎每个交易日在 select 之后调 update + tick；tick 返回 True 表示
    当日处于风控态（强制只卖不买）。状态仅活在单次 run 内。
    """

    def __init__(self, max_drawdown: float | None, cooldown_days: int = 1):
        self._mdd = max_drawdown
        self._cooldown_days = cooldown_days
        self._peak: float | None = None
        self._cooldown = 0

    @property
    def active(self) -> bool:
        return self._cooldown > 0

    def update(self, total_value: float) -> None:
        """更新峰值并判定触发。冷却中不重复触发；冷却后仍超阈值可再触发。"""
        if self._mdd is None or total_value <= 0:
            return
        self._peak = (total_value if self._peak is None
                      else max(self._peak, total_value))
        if self._cooldown == 0 and total_value <= self._peak * (1 - self._mdd):
            self._cooldown = self._cooldown_days
            logger.info(
                "回撤熔断触发: 峰值 %.2f 当前 %.2f 回撤 %.1f%%, 冷却 %d 日",
                self._peak, total_value,
                (1 - total_value / self._peak) * 100, self._cooldown_days,
            )

    def tick(self) -> bool:
        """每个交易日调一次；返回当日是否风控态（冷却计数随之递减）。

        冷却到期时重置峰值，避免现金仓位永久低于阈值导致无限循环触发。
        """
        if self._cooldown > 0:
            self._cooldown -= 1
            if self._cooldown == 0:
                self._peak = None  # 冷却结束，重置峰值以允许策略重新出发
            return True
        return False


def apply_risk_rules(actions: dict, account, total_value: float, rules: dict,
                     industry_fn=None, max_positions: int = 1) -> dict:
    """按规则裁剪/收缩买侧订单（卖侧永不干预），返回新 actions dict。"""
    if not rules or total_value <= 0:
        return actions
    actions = dict(actions)
    _clip_position_pct(actions, total_value, rules)
    _apply_industry_cap(actions, account, total_value, rules,
                        industry_fn, max_positions)
    return actions


def _clip_position_pct(actions: dict, total_value: float, rules: dict) -> None:
    max_pct = rules.get("max_position_pct")
    if max_pct is None:
        return
    cap = max_pct * total_value

    target_value = actions.get("target_value")
    if target_value:
        actions["target_value"] = {
            s: min(v, cap) for s, v in target_value.items()
        }

    buy_conds = actions.get("buy_conditions")
    if buy_conds:
        clipped = []
        for order in buy_conds:
            order = dict(order)
            # shares 口径不 clip: 成交价在撮合前未知，属策略显式数量单
            if order.get("value") is not None:
                order["value"] = min(order["value"], cap)
            clipped.append(order)
        actions["buy_conditions"] = clipped

    weights = actions.get("buy_weights")
    if weights:
        # weights 中的值是权重分数（如 0.14），需转为绝对金额与 cap 比较
        actions["buy_weights"] = {s: min(w * total_value, cap) / total_value
                                   for s, w in weights.items()}


def _apply_industry_cap(actions: dict, account, total_value: float,
                        rules: dict, industry_fn, max_positions: int) -> None:
    pct = rules.get("max_industry_pct")
    if pct is None:
        return
    if industry_fn is None:
        raise ValueError("max_industry_pct 需要 backend 提供 get_stock_industries")
    cap = pct * total_value

    buy = list(actions.get("buy", []))
    weights = actions.get("buy_weights")
    target_value = dict(actions.get("target_value") or {})
    buy_conds = [dict(o) for o in actions.get("buy_conditions") or []]

    symbols = (set(account.holdings) | set(buy) | set(target_value)
               | {o["symbol"] for o in buy_conds})
    industry_map = industry_fn(sorted(symbols)) if symbols else {}

    # 当前各行业市值（持仓 × last_price）；卖出释放的余量当日不回补
    exposure: dict[str, float] = {}
    for symbol, holding in account.holdings.items():
        ind = industry_map.get(symbol)
        if ind is None:
            continue
        exposure[ind] = exposure.get(ind, 0.0) + holding.shares * holding.last_price

    # 名单买单: 行业已超限即丢弃（等权/加权金额不可收缩）
    kept_buy = []
    for symbol in buy:
        ind = industry_map.get(symbol)
        if ind is None:
            kept_buy.append(symbol)
            continue
        if exposure.get(ind, 0.0) >= cap:
            logger.warning("行业 %s 暴露已达上限 %.0f, 丢弃买单 %s",
                           ind, cap, symbol)
            continue
        amount = total_value * (weights[symbol] if weights
                                else 1 / max_positions)
        exposure[ind] = exposure.get(ind, 0.0) + amount
        kept_buy.append(symbol)
    actions["buy"] = kept_buy
    if weights is not None:
        actions["buy_weights"] = {s: weights[s] for s in kept_buy}

    # target_value: 收缩到行业余量（目标口径, 减去该票当前市值后再比）
    for symbol in list(target_value):
        ind = industry_map.get(symbol)
        if ind is None:
            continue
        holding = account.holdings.get(symbol)
        current = holding.shares * holding.last_price if holding else 0.0
        room = cap - (exposure.get(ind, 0.0) - current)
        new_target = min(target_value[symbol], max(room, 0.0))
        if new_target < target_value[symbol]:
            logger.warning("行业 %s 上限: %s 目标市值 %.0f → %.0f",
                           ind, symbol, target_value[symbol], new_target)
        exposure[ind] = exposure.get(ind, 0.0) - current + new_target
        if new_target <= 0 and holding is None:
            del target_value[symbol]
        else:
            target_value[symbol] = new_target
    actions["target_value"] = target_value

    # buy_conditions: value 口径收缩到行业余量; shares 口径不 clip
    kept_conds = []
    for order in buy_conds:
        ind = industry_map.get(order["symbol"])
        if ind is None or order.get("value") is None:
            kept_conds.append(order)
            continue
        room = cap - exposure.get(ind, 0.0)
        if room <= 0:
            logger.warning("行业 %s 暴露已达上限 %.0f, 丢弃条件买单 %s",
                           ind, cap, order["symbol"])
            continue
        order["value"] = min(order["value"], room)
        exposure[ind] = exposure.get(ind, 0.0) + order["value"]
        kept_conds.append(order)
    actions["buy_conditions"] = kept_conds
