"""TDD review tests for match subsystem (scope: btcore/match/*, limits, slippage, costs)."""

from btcore.match.conditions import (
    register_condition_handler,
    validate_condition_types,
)


def test_custom_condition_non_price_required_keys_allows_price_none():
    """F-MAT-01: docs §5.2.3/§8 规定自定义条件单可 price=None（handler 自行计算）。

    register_condition_handler(required_keys={"custom_param"}) 的自定义类型
    不应被 price 正数校验误伤——该校验只应约束 price 为必填键的类型。
    """

    def handler(holding, cond, bar):
        return False, 0.0, {}

    register_condition_handler(
        "REVIEW_CUSTOM", handler, required_keys={"custom_param"}
    )
    cond = {"type": "REVIEW_CUSTOM", "price": None, "custom_param": 3}

    validate_condition_types([cond])
