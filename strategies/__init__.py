"""用户策略层。编写指南见 docs/strategy_guide.md。"""

from btcore.strategy_loader import load_strategy
from btcore.strategy_tools import ConditionBuilder

__all__ = ["ConditionBuilder", "load_strategy"]
