"""ML_EXIT 条件单 handler — 意图中性的离场成交机制。

策略（通常经 ConditionBuilder 的 model_exit 规则）在 T 日生成
{"type": "ML_EXIT", "model", "score"} 条件单，T+1 盘中由本 handler
以开盘价成交——与 MANUAL 卖出的磨损口径一致，享受条件单全部既有
护栏（跌停顺延 / 成交量 cap / T+1 锁定跳过）。

handler 本身无状态、不含决策：分数的含义与阈值都是策略的意图。
"""

from btcore.match.conditions import register_condition_handler
from btcore.match.core import is_valid_price
from btcore.types import bar_get

ML_EXIT = "ML_EXIT"


def handle_ml_exit(holding, cond: dict, bar) -> tuple:
    """ML 离场单：次日开盘价成交；open 非法则不触发（顺延）。"""
    open_price = bar_get(bar, "open", 0.0)
    log = {"model": cond.get("model"), "score": cond.get("score")}
    if not is_valid_price(open_price):
        return False, 0.0, log
    return True, open_price, log


def register() -> None:
    """注册 ML_EXIT handler（loader 检测到 models 声明时调用，幂等）。"""
    register_condition_handler(ML_EXIT, handle_ml_exit)
