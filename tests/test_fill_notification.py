"""成交通知：snapshot.trades 与可选 on_fills hook。

验证 A（快照携带当日成交）+ B（select 前事件回调）：
  - on_fills 每日先于 select 调用，内容与 snapshot.trades 一致
  - 回测首日前的预跑以空列表调用
  - 条件单平仓带 trigger/成交价可见；手动买单含滑点后实际成交价
  - 未定义 on_fills 的鸭子类型策略照常运行（向后兼容）
"""

from btcore.engine import Engine
from btcore.provider import DataProvider
from tests.conftest import MockDataBackend

START, END = "20240603", "20240610"


class FillRecorder:
    """每日全仓买一只候选股，并用高价 STOP_LOSS 强制次日触发条件单。"""

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 2, "max_positions": 1}
        self.events: list[str] = []
        self.fills_log: list[list] = []
        self.snapshot_trades_log: list[list] = []

    def on_start(self, provider, first_date, end_date=None):
        pass

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def on_fills(self, trades, provider):
        self.events.append("fills")
        self.fills_log.append(list(trades))

    def select(self, bars, snapshot, provider):
        self.events.append("select")
        self.snapshot_trades_log.append(list(snapshot.trades))
        current = set(snapshot.holdings.keys())
        candidates = [s for s in bars if s not in current]
        if not current and candidates:
            return {"buy": [candidates[0]], "sell": []}
        return {"buy": [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        # 止损价高于成本价 → 次日开盘必触发（仅为制造确定性条件单成交）
        return [{"type": "STOP_LOSS", "price": entry_price * 1.5}]


def _run(strategy) -> dict:
    provider = DataProvider(MockDataBackend())
    engine = Engine(strategy, provider, initial_capital=1_000_000,
                    db_path=":memory:", max_positions=2)
    return engine.run(START, END)


def test_on_fills_called_before_select_each_day():
    strategy = FillRecorder()
    _run(strategy)

    assert len(strategy.fills_log) == len(strategy.snapshot_trades_log) > 0
    pairs = zip(strategy.events[::2], strategy.events[1::2], strict=True)
    assert all(pair == ("fills", "select") for pair in pairs)


def test_pre_run_fills_empty_then_condition_fills_visible():
    strategy = FillRecorder()
    _run(strategy)

    # 首日前预跑：无成交
    assert strategy.fills_log[0] == []
    # 之后每个决策日应看到前一 pending 的买单成交 + 条件单平仓
    triggers = {t.trigger for fills in strategy.fills_log[1:] for t in fills}
    assert "MANUAL" in triggers
    assert "STOP_LOSS" in triggers

    # snapshot.trades 与 on_fills 收到的是同一份内容
    for fills, snap_trades in zip(
        strategy.fills_log, strategy.snapshot_trades_log, strict=True
    ):
        assert [(t.symbol, t.side, t.trigger, t.price) for t in fills] == [
            (t.symbol, t.side, t.trigger, t.price) for t in snap_trades
        ]


def test_condition_fill_has_price_and_reason():
    strategy = FillRecorder()
    _run(strategy)

    stop_fills = [t for fills in strategy.fills_log for t in fills
                  if t.trigger == "STOP_LOSS"]
    assert stop_fills, "expected at least one condition fill"
    for t in stop_fills:
        assert t.side == "SELL"
        assert t.price > 0
        assert t.shares > 0
        assert t.net_amount > 0


def test_manual_buy_fill_price_includes_slippage():
    no_slip = FillRecorder(config={"slippage_ticks": 0, "max_positions": 1})
    slipped = FillRecorder(config={"slippage_ticks": 2, "max_positions": 1})
    _run(no_slip)
    _run(slipped)

    def first_buy(s):
        return next(t for fills in s.fills_log for t in fills if t.side == "BUY")

    p0 = first_buy(no_slip).price
    p2 = first_buy(slipped).price
    assert p2 > p0, "slippage_ticks=2 的买入成交价应高于 0 滑点"


class NoHookStrategy:
    """不定义 on_fills 的鸭子类型策略，引擎应兼容。"""

    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 1}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def select(self, bars, snapshot, provider):
        current = set(snapshot.holdings.keys())
        candidates = [s for s in bars if s not in current]
        if not current and candidates:
            return {"buy": [candidates[0]], "sell": []}
        return {"buy": [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_strategy_without_on_fills_still_runs():
    result = _run(NoHookStrategy())
    assert len(result["account_daily"]) > 0
    assert len(result["trade_log"]) > 0
