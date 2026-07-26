"""组合级风控测试：risk.py 纯函数/状态机单测 + Engine 端到端。"""

import pytest

from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.risk import DrawdownBreaker, apply_risk_rules, validate_risk_rules
from tests.conftest import MockDataBackend, make_account, make_holding

START, END = "20240603", "20240607"
SYM = "000001.SZ"


def _account(cash=0.0, holdings=None):
    return make_account(cash=cash, initial_capital=100_000.0,
                        holdings=holdings, slippage_ticks=0)


# ── validate_risk_rules ──


def test_validate_accepts_empty():
    assert validate_risk_rules(None) == {}
    assert validate_risk_rules({}) == {}


def test_validate_unknown_key():
    with pytest.raises(ValueError, match="未知 risk_rules 键"):
        validate_risk_rules({"max_leverage": 2})


def test_validate_pct_range():
    with pytest.raises(ValueError, match="max_drawdown"):
        validate_risk_rules({"max_drawdown": 1.5})
    with pytest.raises(ValueError, match="max_position_pct"):
        validate_risk_rules({"max_position_pct": -0.1})


def test_validate_cooldown_requires_drawdown():
    with pytest.raises(ValueError, match="需配合"):
        validate_risk_rules({"cooldown_days": 3})
    rules = validate_risk_rules({"max_drawdown": 0.15, "cooldown_days": 3})
    assert rules["cooldown_days"] == 3


def test_validate_default_cooldown():
    rules = validate_risk_rules({"max_drawdown": 0.15})
    assert rules["cooldown_days"] == 1


# ── DrawdownBreaker ──


def test_breaker_no_trigger_below_threshold():
    b = DrawdownBreaker(0.15, cooldown_days=2)
    b.update(100_000)
    b.update(90_000)  # 回撤 10% < 15%
    assert not b.tick()
    assert not b.active


def test_breaker_trigger_and_cooldown():
    b = DrawdownBreaker(0.15, cooldown_days=2)
    b.update(100_000)
    b.update(85_000)  # 回撤 15% → 触发
    assert b.tick() is True   # 触发日
    assert b.tick() is True   # 冷却第 2 日
    assert b.tick() is False  # 冷却结束
    assert not b.active


def test_breaker_peak_uses_highest():
    b = DrawdownBreaker(0.10, cooldown_days=1)
    b.update(100_000)
    b.update(120_000)   # 新峰值
    b.update(110_000)   # 自峰值回撤 8.3% < 10%
    assert not b.tick()
    b.update(107_000)   # 自峰值回撤 10.8% > 10% → 触发
    assert b.tick() is True


def test_breaker_no_retrigger_during_cooldown():
    b = DrawdownBreaker(0.10, cooldown_days=3)
    b.update(100_000)
    b.update(80_000)  # 触发, 冷却 3 日
    assert b.tick() is True
    b.update(70_000)  # 冷却中继续跌, 不重复触发（cooldown 不被重置）
    assert b.tick() is True
    assert b.tick() is True
    assert b.tick() is False  # 恰好 3 日


def test_breaker_disabled_without_drawdown():
    b = DrawdownBreaker(None)
    b.update(100_000)
    b.update(1_000)
    assert not b.tick()


# ── apply_risk_rules: max_position_pct ──


def test_position_pct_clips_target_value():
    rules = validate_risk_rules({"max_position_pct": 0.10})
    actions = {"buy": [], "sell": [],
               "target_value": {SYM: 50_000.0, "000002.SZ": 5_000.0}}

    out = apply_risk_rules(actions, _account(), 100_000.0, rules)

    assert out["target_value"][SYM] == 10_000.0
    assert out["target_value"]["000002.SZ"] == 5_000.0


def test_position_pct_clips_buy_conditions_value_not_shares():
    rules = validate_risk_rules({"max_position_pct": 0.10})
    actions = {"buy": [], "sell": [], "buy_conditions": [
        {"symbol": SYM, "type": "LIMIT_BUY", "price": 10.0, "value": 50_000.0},
        {"symbol": "000002.SZ", "type": "LIMIT_BUY", "price": 10.0,
         "shares": 100},
    ]}

    out = apply_risk_rules(actions, _account(), 100_000.0, rules)

    assert out["buy_conditions"][0]["value"] == 10_000.0
    assert out["buy_conditions"][1]["shares"] == 100  # shares 口径不动


def test_position_pct_clips_buy_weights():
    rules = validate_risk_rules({"max_position_pct": 0.10})
    actions = {"buy": [SYM], "sell": [], "buy_weights": {SYM: 0.5}}

    out = apply_risk_rules(actions, _account(), 100_000.0, rules)

    assert out["buy_weights"][SYM] == 0.10


# ── apply_risk_rules: max_industry_pct ──


def _industry_fn(mapping):
    return lambda symbols: {s: mapping.get(s) for s in symbols}


def test_industry_cap_drops_buy_list_symbol():
    holding = make_holding(symbol=SYM, shares=5000)
    account = _account(holdings={SYM: holding})  # 银行业已占 50k
    rules = validate_risk_rules({"max_industry_pct": 0.30})
    ind = _industry_fn({SYM: "银行", "000002.SZ": "银行", "000063.SZ": "电子"})
    actions = {"buy": ["000002.SZ", "000063.SZ"], "sell": []}

    out = apply_risk_rules(actions, account, 100_000.0, rules,
                           industry_fn=ind, max_positions=10)

    assert "000002.SZ" not in out["buy"]  # 银行业 50k ≥ 30k 上限 → 丢弃
    assert "000063.SZ" in out["buy"]      # 电子行业有余量 → 保留


def test_industry_cap_clips_target_value_to_room():
    holding = make_holding(symbol=SYM, shares=2000)
    account = _account(holdings={SYM: holding})  # 银行业现 20k
    rules = validate_risk_rules({"max_industry_pct": 0.30})  # cap 30k
    ind = _industry_fn({SYM: "银行"})
    actions = {"buy": [], "sell": [], "target_value": {SYM: 50_000.0}}

    out = apply_risk_rules(actions, account, 100_000.0, rules,
                           industry_fn=ind, max_positions=10)

    assert out["target_value"][SYM] == 30_000.0  # 收缩到行业上限


def test_industry_cap_clips_buy_conditions_value():
    holding = make_holding(symbol=SYM, shares=2000)
    account = _account(holdings={SYM: holding})
    rules = validate_risk_rules({"max_industry_pct": 0.30})
    ind = _industry_fn({SYM: "银行", "000002.SZ": "银行"})
    actions = {"buy": [], "sell": [], "buy_conditions": [
        {"symbol": "000002.SZ", "type": "LIMIT_BUY", "price": 10.0,
         "value": 50_000.0},
    ]}

    out = apply_risk_rules(actions, account, 100_000.0, rules,
                           industry_fn=ind, max_positions=10)

    assert out["buy_conditions"][0]["value"] == 10_000.0  # 余量 30k-20k


def test_industry_cap_never_touches_sells():
    holding = make_holding(symbol=SYM, shares=5000)
    account = _account(holdings={SYM: holding})
    rules = validate_risk_rules({"max_industry_pct": 0.30})
    ind = _industry_fn({SYM: "银行"})
    actions = {"buy": [], "sell": [SYM], "sell_shares": {SYM: 1000}}

    out = apply_risk_rules(actions, account, 100_000.0, rules,
                           industry_fn=ind, max_positions=10)

    assert out["sell"] == [SYM]
    assert out["sell_shares"] == {SYM: 1000}


def test_apply_risk_rules_passthrough_without_rules():
    actions = {"buy": [SYM], "sell": []}
    assert apply_risk_rules(actions, _account(), 100_000.0, {}) is actions


# ── Engine 端到端 ──

SYM2 = "000002.SZ"


class _BaseStrategy:
    def __init__(self, config=None):
        self.config = config or {"slippage_ticks": 0, "max_positions": 10}

    def get_universe(self, provider, start, end):
        return [SYM, SYM2]

    def on_start(self, provider, first_date, end_date=None):
        pass

    def calc_conditions(self, symbol, entry_price, bar, holding_days):
        return []


class DrawdownStrategy(_BaseStrategy):
    """空仓即满仓买 000002.SZ（fixture 上旬持续阴跌，用于触发熔断）。"""

    def select(self, bars, snapshot, provider):
        if SYM2 in snapshot.holdings:
            return {"buy": [], "sell": []}
        return {"buy": [SYM2], "sell": [], "buy_weights": {SYM2: 0.95}}


def test_engine_drawdown_breaker_e2e():
    config = {"slippage_ticks": 0, "max_positions": 10,
              "risk_rules": {"max_drawdown": 0.05, "cooldown_days": 3}}
    provider = DataProvider(MockDataBackend())
    engine = Engine(DrawdownStrategy(config), provider,
                    initial_capital=1_000_000)

    result = engine.run("20240603", "20240620")
    trade_log = result["trade_log"]

    sells = trade_log[trade_log["side"] == "SELL"]
    assert len(sells) >= 1
    assert (sells["trigger"] == "RISK").all()  # 强平单带 RISK 标记

    buys = trade_log[trade_log["side"] == "BUY"]
    # fixture 自 0603 起, 预跑日无数据 → 首笔买入 0604 成交
    assert buys.iloc[0]["date"] == "20240604"
    risk_sell_date = sells.iloc[0]["date"]
    assert risk_sell_date == "20240613"  # 0612 触发, 次日强平
    # 冷却 3 个交易日内无买入; 冷却结束后策略恢复买入
    rebuy = buys[buys["date"] > risk_sell_date]
    assert len(rebuy) >= 1
    assert rebuy.iloc[0]["date"] == "20240618"


class TargetStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        return {"buy": [], "sell": [], "target_value": {SYM: 500_000.0}}


def test_engine_position_pct_clips_target_value():
    config = {"slippage_ticks": 0, "max_positions": 10,
              "risk_rules": {"max_position_pct": 0.01}}  # cap = 10k
    provider = DataProvider(MockDataBackend())
    engine = Engine(TargetStrategy(config), provider,
                    initial_capital=1_000_000)

    engine.run(START, END)

    holding = engine.account.holdings[SYM]
    assert holding.shares * holding.last_price <= 10_000.0


class _IndustryBackend(MockDataBackend):
    def get_stock_industries(self, ts_codes):
        return {s: "银行" for s in ts_codes}


class DualBuyStrategy(_BaseStrategy):
    def select(self, bars, snapshot, provider):
        buy = [s for s in (SYM, SYM2) if s not in snapshot.holdings]
        return {"buy": buy, "sell": []}


def test_engine_industry_cap_drops_second_buy():
    config = {"slippage_ticks": 0, "max_positions": 10,
              "risk_rules": {"max_industry_pct": 0.05}}  # cap=50k < 单笔 100k
    provider = DataProvider(_IndustryBackend())
    engine = Engine(DualBuyStrategy(config), provider,
                    initial_capital=1_000_000)

    engine.run(START, END)

    # 同行业两只票: 第一只入场后行业超限, 第二只被入场闸丢弃
    assert len(engine.account.holdings) == 1


class _NoIndustryBackend(MockDataBackend):
    get_stock_industries = None


def test_engine_industry_rule_requires_backend_method():
    config = {"risk_rules": {"max_industry_pct": 0.30}}
    provider = DataProvider(_NoIndustryBackend())
    with pytest.raises(ValueError, match="get_stock_industries"):
        Engine(DualBuyStrategy(config), provider, initial_capital=1_000_000)
