import logging
from abc import ABC, abstractmethod
from typing import ClassVar

from btcore.filters import StockFilter
from btcore.strategy_tools import ConditionBuilder

logger = logging.getLogger(__name__)


class Strategy(ABC):
    """策略抽象基类。

    引擎只通过钩子与策略交互：on_start（启动初始化）/ on_fills（每日成交回报，
    可选）/ on_tick（每日状态维护，可选）/ select（每日选股下单）/
    calc_conditions（条件单生成）/ get_universe（股票池裁剪）。

    声明式配置的默认接线：
      - FILTER_RULES → 默认 on_start 构建 StockFilter 挂到 self._filter，
        select 中经 self.filter_bars(bars, date_str) 过滤
      - conditions   → 基类 __init__ 构建 ConditionBuilder 挂到 self._cond，
        默认 calc_conditions 委托其翻译为条件单
    子类覆盖 on_start / on_tick / calc_conditions 时必须调用 super() 对应实现
    （或自行维护 self._filter / self._cond），否则声明式规则静默失效。

    FACTOR_SPECS 与 FILTER_RULES 是声明式配置；子类可以在类级别定义默认值，
    也可以在构造时通过 ``factor_specs`` / ``filter_rules`` 传入实例级覆盖。

    FACTOR_SPECS 条目为 {name, weight, ascending}（factors/library.yaml 里的
    因子名）；FACTOR_NODES 是该名单的传递引用闭包 {name: {expr, where?}}，
    由 strategy_loader 挂接——引擎 preload 据此规划数据供给并把因子物化为
    bars 列，策略 select() 直接读列（见 strategy_tools.eval_factor_specs）。
    """

    REQUIRED_FIELDS: ClassVar[list[str]] = [
        "open", "high", "low", "close",
        "vol", "adj_factor",
    ]

    FACTOR_SPECS: ClassVar[list[dict]] = []
    FACTOR_NODES: ClassVar[dict | None] = None
    MODEL_SPECS: ClassVar[list] = []
    """ML 模型声明（btcore.ml.spec.ModelSpec 列表），由 strategy_loader
    依据策略 YAML 的 models 节挂接。scope=panel 的模型分数在 preload
    物化为 ml_<name> 列（可在 FACTOR_SPECS 中按名引用参与评分）；
    scope=holding 的模型由引擎在决策时点逐持仓求值，分数注入持仓的
    bar dict——分数的含义由策略自行解释。"""
    FILTER_RULES: ClassVar[dict] = {}
    CONDITION_FACTORS: ClassVar[set[str]] = set()
    """子类可选声明：calc_conditions / 条件单 handler 读取的因子名（不参与评分）。

    策略加载器据此做交叉校验——若 scoring 因子与此集合有交集，发出 WARNING。
    空集（默认）表示未声明，检查跳过。"""

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
        # 声明式接线：conditions 规则 → ConditionBuilder（默认 calc_conditions 委托）
        self._cond = ConditionBuilder(self.config.get("conditions") or {})
        self._filter: StockFilter | None = None

    def get_universe(
        self, provider, start: str, end: str
    ) -> list[str] | None:
        """返回本策略需要的股票列表。None 表示全市场。

        引擎在 preload 阶段调用，用于裁剪数据加载范围。
        基类默认返回 None（全市场）。
        """
        return None

    def get_factor_universe(
        self, provider, start: str, end: str
    ) -> list[str] | None:
        """返回因子计算所需的股票列表。None 表示沿用 get_universe() 的交易域。

        引擎在 preload 阶段调用，用于决定因子物化的数据范围。
        基类默认返回 None（factor universe = trading universe）。
        若 strategy_loader 检测到 filter_rules.factor_universe 配置项，
        会自动覆盖此方法（类似 index_universe → get_universe 的生成模式）。
        """
        return None

    def on_start(
        self, provider, first_date: str, end_date: str | None = None
    ) -> None:
        """默认实现：FILTER_RULES 非空时构建 StockFilter 挂到 self._filter。

        子类覆盖时必须调用 super().on_start(provider, first_date, end_date)，
        否则 self._filter 不构建、过滤规则失效（filter_bars 会 fail-fast 报错）。
        """
        self._filter = None
        if self.FILTER_RULES:
            self._filter = StockFilter(
                provider.backend, first_date, self.FILTER_RULES, end_date=end_date
            )

    def on_fills(self, trades: list, provider) -> None:
        """可选 hook：每日 select 之前调用，告知当日已成交订单。

        trades 是当日撮合产生的 Trade 列表（trigger 含 MANUAL / TARGET /
        STOP_LOSS / TAKE_PROFIT / TRAILING_TP），无成交时为空列表；
        回测首日前的预跑也会以空列表调用一次。
        用于维护策略自身状态（止损冷却、trailing 锚点重置等），
        同样一份 trades 也可在 select 里经 snapshot.trades 读取。
        """

    def on_tick(self, bars, snapshot, provider) -> dict | None:
        """默认实现：修剪 ConditionBuilder 已平仓标的的 trailing 锚点。

        子类覆盖时必须调用 super().on_tick(bars, snapshot, provider)
        （或在自身实现中自行 prune），否则重入场标的可能沿用旧锚点。
        """
        if snapshot is not None:
            self._cond.prune(set(snapshot.holdings.keys()))
        return None

    @abstractmethod
    def select(self, bars, account_snapshot, provider) -> dict:
        ...

    def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
        """默认实现：委托 ConditionBuilder 翻译 YAML conditions 声明。

        子类可覆盖做程序式扩展（在默认条件单之上增删改）。
        """
        return self._cond.calc(symbol, entry_price, bar, holding_days)

    def filter_bars(self, bars: dict, date_str: str) -> dict:
        """FILTER_RULES 过滤；未配置规则时原样返回。

        on_start 未调用 super().on_start() 时 self._filter 未构建——
        FILTER_RULES 非空则直接报错（fail-fast），避免过滤规则静默失效。
        """
        if self._filter is not None:
            return self._filter.filter(bars, date_str)
        if self.FILTER_RULES:
            raise RuntimeError(
                "FILTER_RULES 已配置但 self._filter 未构建——"
                "on_start 必须调用 super().on_start(provider, first_date, end_date)"
            )
        return bars
