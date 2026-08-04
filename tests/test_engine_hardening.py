"""引擎加固项测试：max_positions 硬上限、非法价格防护、涨跌停舍入、
rebalance 零碎股清仓、run 异常状态落库。"""

import logging
import sqlite3

import pandas as pd
import pytest

from btcore.costs import calc_trade_costs
from btcore.database import init_backtest_db
from btcore.engine import Engine
from btcore.limits import get_limit_prices
from btcore.match import conditions
from btcore.match.manual import manual_buy, manual_sell, rebalance_to_targets
from btcore.provider import DataProvider
from btcore.slippage import apply_slippage
from tests.conftest import MockDataBackend, make_account, make_bar, make_holding

NAN = float("nan")


def _account(cash=1_000_000.0, holdings=None):
    return make_account(cash=cash, holdings=holdings, slippage_ticks=0)


def _holding(symbol="000001.SZ", shares=1000, price=10.0):
    return make_holding(symbol=symbol, shares=shares, entry_price=price)


# ── max_positions 软告警（不再硬截断）──


def test_manual_buy_warns_on_max_positions():
    account = _account()
    bars = {s: make_bar() for s in ("000001.SZ", "000002.SZ", "000003.SZ")}
    trades = manual_buy(account, bars, list(bars), 2,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert len(trades) == 3  # max_positions 不再硬截断
    assert len(account.holdings) == 3


def test_manual_buy_no_longer_blocked_by_max_positions():
    account = _account(holdings={"000001.SZ": _holding()})
    trades = manual_buy(account, {"000002.SZ": make_bar()}, ["000002.SZ"], 2,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert len(trades) == 1  # max_positions 不阻止新买
    assert "000002.SZ" in account.holdings


# ── 非法价格防护 ──


def test_sell_skips_nan_open():
    holding = _holding()
    account = _account(cash=0.0, holdings={"000001.SZ": holding})
    bar = make_bar()
    bar["open"] = NAN
    trades = manual_sell(account, {"000001.SZ": bar}, ["000001.SZ"],
                         get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert account.cash == 0.0
    assert account.holdings["000001.SZ"].shares == 1000


def test_buy_skips_nan_open():
    account = _account()
    bar = make_bar()
    bar["open"] = NAN
    trades = manual_buy(account, {"000001.SZ": bar}, ["000001.SZ"], 10,
                        get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert account.cash == 1_000_000.0


def test_rebalance_skips_nan_open():
    account = _account(holdings={"000001.SZ": _holding()})
    bar = make_bar()
    bar["open"] = NAN
    trades = rebalance_to_targets(account, {"000001.SZ": bar},
                                  {"000001.SZ": 0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)
    assert trades == []
    assert account.holdings["000001.SZ"].shares == 1000


def test_settle_ignores_nan_close():
    class _S:
        config = {}

    engine = Engine(_S(), None, initial_capital=1_000_000, db_path=":memory:")
    engine.account.holdings["000001.SZ"] = _holding()
    conn = init_backtest_db(":memory:")
    try:
        engine._settle("20240603", {"000001.SZ": {"close": NAN}}, [], [], conn)
    finally:
        conn.close()
    holding = engine.account.holdings["000001.SZ"]
    assert holding.last_price == 10.0
    assert engine.account.total_value == engine.account.cash + 1000 * 10.0


# ── 涨跌停推算: 交易所四舍五入口径 ──


def test_limit_rounding_half_up():
    # 10.05 × 1.1 = 11.055 → 11.06 (二进制浮点 + 银行家舍入会错给 11.05)
    # 10.05 × 0.9 = 9.045  → 9.05
    up, down = get_limit_prices("000001.SZ", {"pre_close": 10.05}, "20240603")
    assert up == 11.06
    assert down == 9.05


# ── rebalance 显式清仓: 零碎股一并卖出 ──


def test_rebalance_full_exit_sells_odd_lot():
    account = _account(cash=0.0,
                       holdings={"000001.SZ": _holding(shares=150)})
    trades = rebalance_to_targets(account, {"000001.SZ": make_bar()},
                                  {"000001.SZ": 0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)
    assert trades[0].shares == 150
    assert "000001.SZ" not in account.holdings


def test_rebalance_partial_reduction_keeps_lot_truncation():
    # 目标市值 500 (50 股): 减仓按整手截断, 卖 100 股留 50
    account = _account(cash=0.0,
                       holdings={"000001.SZ": _holding(shares=150)})
    trades = rebalance_to_targets(account, {"000001.SZ": make_bar()},
                                  {"000001.SZ": 500.0}, 10,
                                  get_limit_prices, calc_trade_costs,
                                  apply_slippage)
    assert trades[0].shares == 100
    assert account.holdings["000001.SZ"].shares == 50


# ── run 异常: 状态落 failed ──


class _BoomStrategy:
    def __init__(self, config=None):
        self.config = config or {}

    def on_start(self, provider, first_date, end_date=None):
        pass

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def select(self, bars, snapshot, provider):
        raise RuntimeError("boom")

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def test_run_marks_failed_on_exception(tmp_path):
    db = str(tmp_path / "bt.db")
    provider = DataProvider(MockDataBackend())
    engine = Engine(_BoomStrategy(), provider, initial_capital=1_000_000,
                    db_path=db)
    with pytest.raises(RuntimeError, match="boom"):
        engine.run("20240603", "20240607")

    conn = sqlite3.connect(db)
    try:
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
    finally:
        conn.close()
    assert status == "failed"


# ── select() 协议加固: 名单查重 / 返回类型 / 未知键 / 前视钳制 ──


class _DuckStrategy:
    """最小鸭子策略：select/on_tick 返回可注入结果。"""

    config = {}

    def __init__(self, actions=None, tick_result=None):
        self._actions = actions if actions is not None else {"buy": [], "sell": []}
        self._tick_result = tick_result

    def get_universe(self, provider, start, end):
        return None

    def get_factor_universe(self, provider, start, end):
        return None

    def on_start(self, provider, first_date, end_date=None):
        pass

    def on_fills(self, trades, provider):
        pass

    def on_tick(self, bars, snapshot, provider):
        return self._tick_result

    def select(self, bars, account_snapshot, provider):
        return dict(self._actions)

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


def _make_engine(strategy):
    provider = DataProvider(MockDataBackend())
    return Engine(strategy, provider, db_path=":memory:")


def test_buy_duplicate_symbols_raise():
    """buy 名单重复 symbol → 决策时点 ValueError（双重扣款 + 持仓覆盖的前置拦截）。"""
    engine = _make_engine(_DuckStrategy(
        actions={"buy": ["000001.SZ", "000001.SZ"], "sell": []}
    ))
    with pytest.raises(ValueError, match="重复 symbol"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_sell_duplicate_symbols_raise():
    engine = _make_engine(_DuckStrategy(
        actions={"buy": [], "sell": ["000001.SZ", "000001.SZ"]}
    ))
    with pytest.raises(ValueError, match="重复 symbol"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_select_non_dict_raises():
    class NotDict(_DuckStrategy):
        def select(self, bars, account_snapshot, provider):
            return None

    engine = _make_engine(NotDict())
    with pytest.raises(ValueError, match="必须返回 dict"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_select_unknown_key_warns(caplog):
    """select 返回未知键（typo 如 buy_condition）→ WARNING 而不是静默失效。"""
    engine = _make_engine(_DuckStrategy(
        actions={"buy": [], "sell": [], "buy_condition": ["000001.SZ"]}
    ))
    with caplog.at_level(logging.WARNING):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])
    assert "未知键" in caplog.text


def test_on_tick_non_dict_raises():
    engine = _make_engine(_DuckStrategy(tick_result=[]))
    with pytest.raises(ValueError, match="on_tick"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_condition_missing_price_fails_fast():
    """条件单缺必填键（STOP_LOSS 无 price）→ 决策时点 ValueError，不拖到次日撮合。"""

    class NoPriceCond(_DuckStrategy):
        def calc_conditions(self, symbol, entry_price, bar, holding_days):
            return [{"type": "STOP_LOSS"}]

    engine = _make_engine(NoPriceCond())
    engine.account.holdings["000001.SZ"] = make_holding(
        symbol="000001.SZ", shares=1000
    )
    with pytest.raises(ValueError, match="缺必填键"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_provider_clamp_blocks_future():
    """set_as_of 后 provider 查询端被钳制；未钳制时同一查询可读到未来。"""
    provider = DataProvider(MockDataBackend())
    idx = pd.MultiIndex.from_tuples(
        [(d, "000001.SZ") for d in ("20240501", "20240603", "20240604", "20240605")],
        names=["trade_date", "symbol"],
    )
    df = pd.DataFrame({"close": [1.0, 2.0, 3.0, 4.0]}, index=idx)
    provider.attach_bars(df)

    unclamped = provider.get_historical_bars(None, "20240610", lookback_days=1000)
    assert {"20240604", "20240605"} <= set(
        unclamped.index.get_level_values("trade_date")
    )

    provider.set_as_of("20240604")
    clamped = provider.get_historical_bars(None, "20240610", lookback_days=1000)
    dates = set(clamped.index.get_level_values("trade_date"))
    assert dates == {"20240501", "20240603"}


def test_on_start_runs_with_clamped_asof():
    """run() 在 on_start 前已钳制 provider（前视窗口闭合）。"""
    seen = {}

    def check(provider):
        seen["as_of"] = provider.get_as_of()

    strategy = _DuckStrategy()
    strategy.on_start = lambda provider, first_date, end_date=None: check(provider)
    engine = _make_engine(strategy)
    engine.run("20240603", "20240607")
    assert seen["as_of"] is not None
    assert seen["as_of"] <= "20240603"


def test_run_idempotent_second_run_resets_account():
    """同一实例二次 run() 从头重置账户，两次结果完全一致。"""
    strategy = _DuckStrategy(actions={"buy": ["000001.SZ"], "sell": []})
    engine = _make_engine(strategy)
    engine.run("20240603", "20240607")
    assert engine.account.cash < engine.initial_capital  # 发生了买入
    first = (engine.account.cash, len(engine.account.holdings))
    engine.run("20240603", "20240607")
    second = (engine.account.cash, len(engine.account.holdings))
    assert first == second


# ── 配置值校验: slippage_ticks / order_volume_ratio ──


def _cfg_engine(config):
    class S(_DuckStrategy):
        pass

    S.config = config
    return Engine(S(), None, db_path=":memory:")


def test_slippage_ticks_invalid_raises():
    """负值/小数/字符串/bool 滑点配置 → 构造期 ValueError（负滑点会静默虚增收益）。"""
    for bad in (-1, 1.5, "2", True):
        with pytest.raises(ValueError, match="slippage_ticks"):
            _cfg_engine({"slippage_ticks": bad})


def test_slippage_ticks_valid_accepted():
    eng = _cfg_engine({"slippage_ticks": 0})
    assert eng._slippage_ticks == 0
    eng2 = _cfg_engine({})
    assert eng2._slippage_ticks == 2


def test_order_volume_ratio_invalid_raises():
    """负/零/字符串/bool/NaN → 构造期 ValueError（字符串会在撮合期裸崩，负值静默跳过全部订单）。"""
    for bad in (-0.05, 0, "0.05", True, NAN):
        with pytest.raises(ValueError, match="order_volume_ratio"):
            _cfg_engine({"order_volume_ratio": bad})


def test_order_volume_ratio_valid_accepted():
    eng = _cfg_engine({"order_volume_ratio": 0.05})
    assert eng.order_volume_ratio == 0.05
    eng2 = _cfg_engine({})
    assert eng2.order_volume_ratio is None


# ── target_value 值校验 ──


def test_target_value_nan_raises():
    """NaN 目标市值 → 决策时点 ValueError（此前静默零成交零告警）。"""
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": {"000001.SZ": NAN}}))
    with pytest.raises(ValueError, match="target_value"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_target_value_negative_raises():
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": {"000001.SZ": -1.0}}))
    with pytest.raises(ValueError, match="target_value"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_target_value_non_numeric_raises():
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": {"000001.SZ": "1e5"}}))
    with pytest.raises(ValueError, match="target_value"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_target_value_bool_raises():
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": {"000001.SZ": True}}))
    with pytest.raises(ValueError, match="target_value"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_target_value_empty_key_raises():
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": {"": 100.0}}))
    with pytest.raises(ValueError, match="非空字符串"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_target_value_non_dict_raises():
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": [("000001.SZ", 100.0)]}))
    with pytest.raises(ValueError, match="必须是"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_target_value_valid_and_zero_pass():
    """合法 target_value（含 0 = 清仓）正常通过并进入 pending。"""
    engine = _make_engine(_DuckStrategy(
        actions={"target_value": {"000001.SZ": 0.0, "000002.SZ": 50000}}))
    engine.compute_pending(
        "20240603",
        {"000001.SZ": make_bar(), "000002.SZ": make_bar()}, [])
    assert engine.pending_actions["target_value"] == {
        "000001.SZ": 0.0, "000002.SZ": 50000}


# ── on_tick 协议: 只支持 buy_conditions ──


def test_on_tick_extra_keys_raise():
    """on_tick 返回 buy 等合法 select 键 → ValueError（此前静默丢弃）。"""
    engine = _make_engine(_DuckStrategy(tick_result={"buy": ["000001.SZ"]}))
    with pytest.raises(ValueError, match="只支持返回 buy_conditions"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


def test_on_tick_buy_conditions_merged():
    """on_tick 返回 buy_conditions 仍正常合并进 pending。"""
    engine = _make_engine(_DuckStrategy(tick_result={
        "buy_conditions": [{"symbol": "000002.SZ", "type": "LIMIT_BUY",
                             "price": 9.0, "value": 10000}]}))
    engine.compute_pending(
        "20240603",
        {"000001.SZ": make_bar(), "000002.SZ": make_bar()}, [])
    conds = engine.pending_actions["buy_conditions"]
    assert len(conds) == 1
    assert conds[0]["symbol"] == "000002.SZ"


# ── 无当日行情（停牌/缺数据）跳过告警 ──


def test_manual_sell_warns_missing_bar(caplog):
    account = _account(cash=0.0, holdings={"000001.SZ": _holding()})
    with caplog.at_level(logging.WARNING):
        trades = manual_sell(account, {}, ["000001.SZ"],
                             get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert "无当日行情" in caplog.text


def test_manual_buy_warns_missing_bar(caplog):
    account = _account()
    with caplog.at_level(logging.WARNING):
        trades = manual_buy(account, {}, ["000001.SZ"], 10,
                            get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert "无当日行情" in caplog.text


def test_rebalance_warns_missing_bar(caplog):
    account = _account(cash=0.0, holdings={"000001.SZ": _holding()})
    with caplog.at_level(logging.WARNING):
        trades = rebalance_to_targets(account, {}, {"000001.SZ": 0}, 10,
                                      get_limit_prices, calc_trade_costs,
                                      apply_slippage)
    assert trades == []
    assert "无当日行情" in caplog.text


def test_exit_conditions_warns_missing_bar(caplog):
    holding = _holding()
    holding.conditions = [{"type": "STOP_LOSS", "price": 9.0}]
    account = _account(cash=0.0, holdings={"000001.SZ": holding})
    with caplog.at_level(logging.WARNING):
        trades = conditions.exit_conditions(
            account, {}, get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert "无当日行情" in caplog.text


def test_entry_conditions_warns_missing_bar(caplog):
    account = _account()
    order = {"symbol": "000001.SZ", "type": "LIMIT_BUY", "price": 9.0,
             "value": 10000}
    with caplog.at_level(logging.WARNING):
        trades = conditions.entry_conditions(
            account, {}, [order], 10,
            get_limit_prices, calc_trade_costs, apply_slippage)
    assert trades == []
    assert "无当日行情" in caplog.text


# ── select() 名单: 空字符串 symbol ──


def test_select_empty_symbol_raises():
    engine = _make_engine(_DuckStrategy(actions={"buy": [""], "sell": []}))
    with pytest.raises(ValueError, match="空元素"):
        engine.compute_pending("20240603", {"000001.SZ": make_bar()}, [])


# ── step 状态回滚完整性 ──


def test_restore_state_rolls_back_full_account():
    """异常回滚完整：现金/持仓/净值/盈亏/pending/as_of 全部还原。"""
    engine = _make_engine(_DuckStrategy())
    engine.pending_actions = {"buy": ["000001.SZ"], "sell": []}
    engine._save_state()
    engine.account.cash = 0.0
    engine.account.total_value = 1.0
    engine.account.daily_pnl = 2.0
    engine.account.cumulative_pnl = 3.0
    engine.account.holdings["000001.SZ"] = _holding()
    engine.pending_actions = {"buy": [], "sell": []}
    engine.provider.set_as_of("20991231")
    engine._restore_state()
    assert engine.account.cash == engine.initial_capital
    assert engine.account.total_value == engine.initial_capital
    assert engine.account.daily_pnl == 0.0
    assert engine.account.cumulative_pnl == 0.0
    assert engine.account.holdings == {}
    assert engine.pending_actions == {"buy": ["000001.SZ"], "sell": []}
    assert engine.provider.get_as_of() is None


# ── run 异常: KeyboardInterrupt 也标记 failed ──


class _KillStrategy(_BoomStrategy):
    def select(self, bars, snapshot, provider):
        raise KeyboardInterrupt


def test_run_marks_failed_on_keyboard_interrupt(tmp_path):
    db = str(tmp_path / "ki.db")
    provider = DataProvider(MockDataBackend())
    engine = Engine(_KillStrategy(), provider, initial_capital=1_000_000,
                    db_path=db)
    with pytest.raises(KeyboardInterrupt):
        engine.run("20240603", "20240607")

    conn = sqlite3.connect(db)
    try:
        status = conn.execute("SELECT status FROM runs").fetchone()[0]
    finally:
        conn.close()
    assert status == "failed"
