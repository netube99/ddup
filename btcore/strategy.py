import logging
from abc import ABC, abstractmethod
from typing import ClassVar

logger = logging.getLogger(__name__)


class Strategy(ABC):
    """策略抽象基类。

    引擎只通过钩子与策略交互：on_start（启动初始化）/ select（每日选股下单）/
    calc_conditions（条件单生成）/ get_universe（股票池裁剪），以及可选的
    on_fills（每日成交回报通知）。

    FACTOR_SPECS 与 FILTER_RULES 是声明式配置；子类可以在类级别定义默认值，
    也可以在构造时通过 ``factor_specs`` / ``filter_rules`` 传入实例级覆盖。

    FACTOR_SPECS 条目为 {name, weight, ascending}（factors/library.yaml 里的
    因子名）；FACTOR_NODES 是该名单的传递引用闭包 {name: {expr, where?}}，
    由 strategy_loader 挂接——引擎 preload 据此规划数据供给并把因子物化为
    bars 列，策略 select() 直接读列（见 strategy_tools.eval_factor_specs）。
    """

    REQUIRED_FIELDS: ClassVar[list[str]] = [
        "open", "high", "low", "close",
        "vol", "amount", "adj_factor",
    ]

    FACTOR_SPECS: ClassVar[list[dict]] = []
    FACTOR_NODES: ClassVar[dict | None] = None
    FILTER_RULES: ClassVar[dict] = {}

    def __init__(
        self,
        config: dict,
        factor_specs: list[dict] | None = None,
        filter_rules: dict | None = None,
    ):
        self.config = config
        self.FACTOR_SPECS = (
            list(factor_specs) if factor_specs is not None else list(self.FACTOR_SPECS)
        )
        self.FILTER_RULES = (
            dict(filter_rules) if filter_rules is not None else dict(self.FILTER_RULES)
        )

    def get_universe(
        self, provider, start: str, end: str
    ) -> list[str] | None:
        """返回本策略需要的股票列表。None 表示全市场。

        引擎在 preload 阶段调用，用于裁剪数据加载范围。
        基类默认返回 None（全市场）。
        """
        return None

    @abstractmethod
    def on_start(
        self, provider, first_date: str, end_date: str | None = None
    ) -> None:
        ...

    def on_fills(self, trades: list, provider) -> None:
        """可选 hook：每日 select 之前调用，告知当日已成交订单。

        trades 是当日撮合产生的 Trade 列表（trigger 含 MANUAL / TARGET /
        STOP_LOSS / TAKE_PROFIT / TRAILING_TP），无成交时为空列表；
        回测首日前的预跑也会以空列表调用一次。
        用于维护策略自身状态（止损冷却、trailing 锚点重置等），
        同样一份 trades 也可在 select 里经 snapshot.trades 读取。
        """

    @abstractmethod
    def select(self, bars, account_snapshot, provider) -> dict:
        ...

    @abstractmethod
    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        ...
