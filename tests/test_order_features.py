"""订单能力测试：sell_shares 部分卖出 / buy_conditions 条件买入 /
Snapshot 加固 / execution_price 成交价可选。"""

import pytest

from btcore.costs import calc_trade_costs
from btcore.engine import Engine
from btcore.limits import get_limit_prices
from btcore.match.conditions import entry_conditions
from btcore.match.manual import manual_buy
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage
from tests.conftest import MockDataBackend, make_account, make_bar, make_holding

START, END = "20240603", "20240607"
SYM = "000001.SZ"


def _account(cash=100_000.0, holdings=None):
    return make_account(cash=cash, holdings=holdings, slippage_ticks=0)


class _BaseStrategy:
    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 10}

    def get_universe(self, provider, start, end):
        return [SYM]

    def get_factor_universe(self, provider, start, end):
        return None

    def on_start(self, provider, first_date, end_date=None):
        pass

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


# ── sell_shares 部分卖出 ──


class PartialSellStrategy(_BaseStrategy):
    """首日买入；持仓后卖一次, 留 100 股。"""

    def __init__(self):
        super().__init__()
        self._sold = False

    def select(self, bars, snapshot, provider):
        holding = snapshot.holdings.get(SYM)
        if holding is None:
            return {"buy": [SYM], "sell": []}
        if not self._sold and holding.shares > 100:
            self._sold = True
            return {"buy": [], "sell": [SYM],
                    "sell_shares": {SYM: holding.shares - 100}}
        return {"buy": [], "sell": []}


def test_engine_sell_shares_partial():
    provider = DataProvider(MockDataBackend())
    engine = Engine(PartialSellStrategy(), provider, initial_capital=1_000_000)

    result = engine.run(START, END)

    sell_log = result["trade_log"]
    sell_log = sell_log[sell_log["side"] == "SELL"]
    assert len(sell_log) == 1
    holding = engine.account.holdings[SYM]
    assert holding.shares == 100
    # 成本按比例缩减
    assert holding.cost == pytest.approx(holding.entry_price * 100)


class BadSellSharesStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        return {"buy": [], "sell": [SYM], "sell_shares": {"000002.SZ": 100}}


def test_sell_shares_symbol_must_be_in_sell():
    provider = DataProvider(MockDataBackend())
    engine = Engine(BadSellSharesStrategy(), provider,
                    initial_capital=1_000_000)
    with pytest.raises(ValueError, match="不在 sell 名单"):
        engine.run(START, END)


# ── buy_conditions 撮合层 ──


def _limit_order(price=10.0, **kw):
    return {"symbol": SYM, "type": "LIMIT_BUY", "price": price, **kw}


def test_limit_buy_fills_at_open_when_open_below_limit():
    account = _account()
    bars = {SYM: make_bar(open=9.5, low=9.0, high=10.0)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert len(trades) == 1
    assert trades[0].price == 9.5  # 低开按 open 成交
    assert trades[0].trigger == "LIMIT_BUY"
    assert account.holdings[SYM].locked is True  # T+1


def test_limit_buy_fills_at_limit_price_intraday():
    account = _account()
    bars = {SYM: make_bar(open=10.5, low=9.8, high=10.6)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert len(trades) == 1
    assert trades[0].price == 10.0  # 盘中触价按限价成交
    assert trades[0].shares == 500


def test_limit_buy_not_triggered():
    account = _account()
    bars = {SYM: make_bar(open=10.5, low=10.2, high=10.6)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert trades == []
    assert SYM not in account.holdings


def test_breakout_buy_fills_at_trigger_price():
    account = _account()
    bars = {SYM: make_bar(open=9.8, low=9.5, high=10.5)}

    trades = entry_conditions(
        account, bars,
        [{"symbol": SYM, "type": "BREAKOUT_BUY", "price": 10.0,
          "value": 5000.0}],
        10, get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].price == 10.0
    assert trades[0].trigger == "BREAKOUT_BUY"


def test_breakout_buy_fills_at_open_when_gapped():
    account = _account()
    bars = {SYM: make_bar(open=10.4, low=10.1, high=10.6)}

    trades = entry_conditions(
        account, bars,
        [{"symbol": SYM, "type": "BREAKOUT_BUY", "price": 10.0,
          "value": 5000.0}],
        10, get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].price == 10.4  # 跳空高开按 open 成交


def test_buy_condition_limit_up_skip():
    account = _account()
    # open 即涨停价: 触发但 fill >= up_limit → 不买
    bars = {SYM: make_bar(open=11.0, low=11.0, high=11.0,
                          up_limit=11.0, down_limit=9.0)}

    trades = entry_conditions(
        account, bars,
        [{"symbol": SYM, "type": "BREAKOUT_BUY", "price": 10.0,
          "value": 5000.0}],
        10, get_limit_prices, calc_trade_costs, apply_slippage)

    assert trades == []


def test_buy_condition_max_positions():
    existing = make_holding(symbol="000002.SZ", shares=100)
    account = _account(holdings={"000002.SZ": existing})
    bars = {SYM: make_bar(open=9.5, low=9.0)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              1, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert trades == []
    assert SYM not in account.holdings


def test_buy_condition_cash_shrink():
    account = _account(cash=1500.0)
    bars = {SYM: make_bar(open=9.5, low=9.0)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert len(trades) == 1
    assert trades[0].shares == 100  # 现金只够 100 股
    assert account.cash >= 0


def test_buy_condition_shares_sizing_normalized():
    account = _account()
    bars = {SYM: make_bar(open=9.5, low=9.0)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, shares=250)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert len(trades) == 1
    assert trades[0].shares == 200  # 非整手向下取整


def test_buy_condition_skips_existing_holding():
    existing = make_holding(symbol=SYM, shares=100)
    account = _account(holdings={SYM: existing})
    bars = {SYM: make_bar(open=9.5, low=9.0)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert trades == []
    assert account.holdings[SYM].shares == 100


# ── buy_conditions 引擎接线与校验 ──


class LimitBuyStrategy(_BaseStrategy):
    """每日声明一个必触发的限价买单（price 极高 → 按 open 成交）。"""

    def __init__(self):
        super().__init__()
        self.fill_triggers = []

    def select(self, bars, snapshot, provider):
        if SYM in snapshot.holdings:
            return {"buy": [], "sell": []}
        return {"buy": [], "sell": [],
                "buy_conditions": [_limit_order(99999.0, value=50000.0)]}

    def on_fills(self, trades, provider):
        self.fill_triggers.extend(t.trigger for t in trades)


def test_engine_buy_conditions_e2e():
    provider = DataProvider(MockDataBackend())
    strategy = LimitBuyStrategy()
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    result = engine.run(START, END)

    trade_log = result["trade_log"]
    assert (trade_log["trigger"] == "LIMIT_BUY").all()
    assert SYM in engine.account.holdings
    assert "LIMIT_BUY" in strategy.fill_triggers  # on_fills 次日可见


class ConflictBuyCondStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        return {"buy": [], "sell": [SYM],
                "buy_conditions": [_limit_order(10.0, value=1000.0)]}


def test_buy_conditions_conflict_with_sell():
    provider = DataProvider(MockDataBackend())
    engine = Engine(ConflictBuyCondStrategy(), provider,
                    initial_capital=1_000_000)
    with pytest.raises(ValueError, match="冲突"):
        engine.run(START, END)


class MutexTargetBuyCondStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        return {"buy": [], "sell": [],
                "target_value": {SYM: 1000.0},
                "buy_conditions": [_limit_order(10.0, value=1000.0)]}


def test_buy_conditions_mutex_with_target_value():
    provider = DataProvider(MockDataBackend())
    engine = Engine(MutexTargetBuyCondStrategy(), provider,
                    initial_capital=1_000_000)
    with pytest.raises(ValueError, match="互斥"):
        engine.run(START, END)


class UnknownTypeStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        return {"buy": [], "sell": [],
                "buy_conditions": [{"symbol": SYM, "type": "ICEBERG",
                                    "price": 10.0, "value": 1000.0}]}


def test_buy_conditions_unknown_type_fails_fast():
    provider = DataProvider(MockDataBackend())
    engine = Engine(UnknownTypeStrategy(), provider, initial_capital=1_000_000)
    with pytest.raises(ValueError, match="未注册的条件买入类型"):
        engine.run(START, END)


class BadSizingStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        return {"buy": [], "sell": [],
                "buy_conditions": [_limit_order(10.0, value=1000.0,
                                                shares=100)]}


def test_buy_conditions_value_and_shares_mutex():
    provider = DataProvider(MockDataBackend())
    engine = Engine(BadSizingStrategy(), provider, initial_capital=1_000_000)
    with pytest.raises(ValueError, match="恰填一个"):
        engine.run(START, END)


# ── Snapshot 加固 ──


class SnapshotProbeStrategy(_BaseStrategy):
    """记录每日 snapshot.total_value; 有持仓时篡改 snapshot 持仓股数。"""

    def __init__(self):
        super().__init__()
        self.total_values = []

    def select(self, bars, snapshot, provider):
        self.total_values.append(snapshot.total_value)
        holding = snapshot.holdings.get(SYM)
        if holding is not None:
            holding.shares = 1  # 恶意篡改, 不应影响引擎
        if SYM not in snapshot.holdings:
            return {"buy": [SYM], "sell": []}
        return {"buy": [], "sell": []}


def test_snapshot_total_value_and_mutation_isolation():
    provider = DataProvider(MockDataBackend())
    strategy = SnapshotProbeStrategy()
    engine = Engine(strategy, provider, initial_capital=1_000_000)

    engine.run(START, END)

    # 首日预跑时 total_value = 初始资金; 末日与账户一致
    assert strategy.total_values[0] == pytest.approx(1_000_000)
    assert strategy.total_values[-1] == pytest.approx(
        engine.account.total_value)
    # 篡改不污染引擎持仓
    assert engine.account.holdings[SYM].shares > 100


# ── execution_price ──


def test_manual_buy_close_execution():
    account = _account()
    account.execution_price = "close"
    bars = {SYM: make_bar(open=10.0, close=10.5)}

    trades = manual_buy(account, bars, [SYM], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)

    assert len(trades) == 1
    assert trades[0].price == 10.5  # 按收盘价成交（slippage_ticks=0）


def test_execution_price_config_validation():
    provider = DataProvider(MockDataBackend())
    strategy = _BaseStrategy(config={"execution_price": "vwap"})
    with pytest.raises(ValueError, match="execution_price"):
        Engine(strategy, provider, initial_capital=1_000_000)


class CloseBuyStrategy(_BaseStrategy):
    def __init__(self):
        super().__init__(config={"slippage_ticks": 0, "max_positions": 10,
                                 "execution_price": "close"})

    def select(self, bars, snapshot, provider):
        if SYM not in snapshot.holdings:
            return {"buy": [SYM], "sell": []}
        return {"buy": [], "sell": []}


def test_engine_close_execution_e2e():
    provider = DataProvider(MockDataBackend())
    engine = Engine(CloseBuyStrategy(), provider, initial_capital=1_000_000)

    result = engine.run(START, END)

    trade_log = result["trade_log"]
    assert len(trade_log) == 1
    fill_date = trade_log.iloc[0]["date"]
    close_price = engine.bars_by_date[fill_date].loc[SYM]["close"]
    assert trade_log.iloc[0]["price"] == pytest.approx(close_price)


# ── buy_weights 加权买入 ──

SYM2 = "000002.SZ"


def test_manual_buy_with_weights():
    account = _account()
    bars = {SYM: make_bar(open=10.0), SYM2: make_bar(open=20.0)}

    trades = manual_buy(account, bars, [SYM, SYM2], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage,
                        weights_map={SYM: 0.5, SYM2: 0.1})

    assert len(trades) == 2
    # 总资产 100k: SYM 50k → 5000 股; SYM2 10k → 500 股
    assert trades[0].shares == 5000
    assert trades[1].shares == 500


class WeightedBuyStrategy(_BaseStrategy):
    def __init__(self, weights=None):
        super().__init__()
        self._weights = weights if weights is not None else {SYM: 0.2,
                                                             SYM2: 0.1}

    def get_universe(self, provider, start, end):
        return [SYM, SYM2]

    def select(self, bars, snapshot, provider):
        held = set(snapshot.holdings)
        buy = [s for s in (SYM, SYM2) if s not in held]
        return {"buy": buy, "sell": [],
                "buy_weights": {s: self._weights[s] for s in buy}}


def test_engine_buy_weights_e2e():
    provider = DataProvider(MockDataBackend())
    engine = Engine(WeightedBuyStrategy(), provider, initial_capital=1_000_000)

    result = engine.run(START, END)

    trade_log = result["trade_log"]
    assert len(trade_log) == 2
    turnovers = dict(zip(trade_log["symbol"], trade_log["turnover"],
                         strict=True))
    # 权重 0.2 vs 0.1 → 成交额约 2:1
    assert turnovers[SYM] == pytest.approx(turnovers[SYM2] * 2, rel=0.05)


def test_buy_weights_sum_over_one_fails():
    provider = DataProvider(MockDataBackend())
    strategy = WeightedBuyStrategy(weights={SYM: 0.8, SYM2: 0.5})
    engine = Engine(strategy, provider, initial_capital=1_000_000)
    with pytest.raises(ValueError, match="权重之和"):
        engine.run(START, END)


def test_buy_weights_key_mismatch_fails():
    class MismatchStrategy(_BaseStrategy):
        def get_universe(self, provider, start, end):
            return [SYM, SYM2]

        def select(self, bars, snapshot, provider):
            if snapshot.holdings:
                return {"buy": [], "sell": []}
            return {"buy": [SYM, SYM2], "sell": [],
                    "buy_weights": {SYM: 0.2}}  # 缺 SYM2

    provider = DataProvider(MockDataBackend())
    engine = Engine(MismatchStrategy(), provider, initial_capital=1_000_000)
    with pytest.raises(ValueError, match="与 buy 名单一致"):
        engine.run(START, END)


# ── 条件单独立滑点档数（condition_slippage_ticks）──


def test_entry_condition_slip_ticks_override():
    account = _account()  # slippage_ticks=0
    bars = {SYM: make_bar(open=10.5, low=9.8, high=10.6)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage, slip_ticks=3)

    assert len(trades) == 1
    assert trades[0].price == 10.03  # 10.0 + 3 档, 不用 account 的 0 档


def test_entry_condition_slip_ticks_default_falls_back():
    account = make_account(cash=100_000.0, slippage_ticks=2)
    bars = {SYM: make_bar(open=10.5, low=9.8, high=10.6)}

    trades = entry_conditions(account, bars, [_limit_order(10.0, value=5000.0)],
                              10, get_limit_prices, calc_trade_costs,
                              apply_slippage)

    assert trades[0].price == 10.02  # 回退 account.slippage_ticks


class CondSlipStrategy(_BaseStrategy):
    """每日全仓买一只未持仓股, 高价 STOP_LOSS 强制次日触发条件单。"""

    def select(self, bars, snapshot, provider):
        current = set(snapshot.holdings.keys())
        candidates = [s for s in bars if s not in current]
        if not current and candidates:
            return {"buy": [candidates[0]], "sell": []}
        return {"buy": [], "sell": []}

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        # 止损价高于成本价 → 次日开盘必触发（仅为制造确定性条件单成交）
        return [{"type": "STOP_LOSS", "price": entry_price * 1.5}]


def _run_cond_slip(condition_ticks):
    config = {"slippage_ticks": 0, "max_positions": 1,
              "condition_slippage_ticks": condition_ticks}
    provider = DataProvider(MockDataBackend())
    engine = Engine(CondSlipStrategy(config), provider,
                    initial_capital=1_000_000)
    result = engine.run(START, END)
    log = result["trade_log"]
    return log[log["trigger"] == "STOP_LOSS"]


def test_engine_condition_slippage_ticks_wiring():
    base = _run_cond_slip(0)
    slipped = _run_cond_slip(3)

    assert len(base) > 0
    assert len(base) == len(slipped)
    for t0, t3 in zip(base.itertuples(), slipped.itertuples(), strict=True):
        assert t0.symbol == t3.symbol
        assert t3.price == pytest.approx(t0.price - 0.03)


def test_engine_condition_slippage_ticks_validation():
    for bad in (-1, 1.5, True):
        config = {"slippage_ticks": 0, "max_positions": 10,
                  "condition_slippage_ticks": bad}
        provider = DataProvider(MockDataBackend())
        with pytest.raises(ValueError, match="condition_slippage_ticks"):
            Engine(CondSlipStrategy(config), provider)
