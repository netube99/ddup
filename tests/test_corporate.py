import pytest

from btcore.corporate import adjust, derived_trades
from tests.conftest import MockDataBackend, make_account, make_holding


class FakeProvider:
    def get_dividends_on_date(self, date_str):
        return self._dividends.get(date_str, {})


def test_stk_div_increases_shares():
    holding = make_holding(shares=1000, entry_date="20240601")
    account = make_account(cash=100_000.0, holdings={"000001.SZ": holding})
    provider = FakeProvider()
    provider._dividends = {"20240615": {"000001.SZ": {"stk_div": 0.5, "cash_div": 0.0}}}

    log = []
    adjust(account, "20240615", {}, provider, log)

    assert holding.shares == 1500
    assert holding.cost == 10000.0
    assert len(log) == 1
    assert log[0]["type"] == "stk_div"


def test_cash_div_with_tax_short_term():
    holding = make_holding(shares=1000, entry_date="20240601")
    account = make_account(cash=100_000.0, holdings={"000001.SZ": holding})
    provider = FakeProvider()
    provider._dividends = {"20240615": {"000001.SZ": {"stk_div": 0.0, "cash_div": 0.5}}}

    log = []
    adjust(account, "20240615", {}, provider, log)

    assert log[0]["tax_rate"] == 0.20
    net = 0.5 * 1000 * 0.8
    assert account.cash == 100_000.0 + net
    assert holding.cost == 10000.0 - net
    assert log[0]["type"] == "cash_div"


def test_cash_div_long_term_no_tax():
    holding = make_holding(shares=1000, entry_date="20230101")
    account = make_account(cash=100_000.0, holdings={"000001.SZ": holding})
    provider = FakeProvider()
    provider._dividends = {"20240615": {"000001.SZ": {"stk_div": 0.0, "cash_div": 0.5}}}

    log = []
    adjust(account, "20240615", {}, provider, log)

    assert log[0]["tax_rate"] == 0.0


def test_missing_dividend_skips():
    holding = make_holding(shares=1000, entry_date="20240601")
    account = make_account(cash=100_000.0, holdings={"000001.SZ": holding})
    provider = FakeProvider()
    provider._dividends = {}

    log = []
    adjust(account, "20240615", {}, provider, log)

    assert holding.shares == 1000
    assert len(log) == 0


def test_engine_logs_stk_div_trade(tmp_path):
    """引擎把送转增股写入 trade_log（side=STK_DIV, shares=送转后总股数）。

    fixtures 中 920469.BJ 于 20240611 除权（stk_div=0.3）；买入当天除权不
    享有分红（先 corporate.adjust 后撮合），必须跨除权日持仓。跨除权日的
    stats 往返盈亏 / Brinson 重建 / ML 回合配对都依赖这行记录。
    """
    from btcore.engine import Engine
    from btcore.provider import DataProvider
    from btcore.strategy import Strategy
    from tests.conftest import MockDataBackend

    class HoldThroughSplit(Strategy):
        REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]

        def __init__(self, **kw):
            super().__init__(config=kw.pop("config", {}), **kw)

        def on_start(self, provider, first_date, end_date=None):
            pass

        def select(self, bars, snapshot, provider) -> dict:
            return {"buy": ["920469.BJ"], "sell": []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    backend = MockDataBackend()
    provider = DataProvider(backend)
    db_path = tmp_path / "run.db"
    strategy = HoldThroughSplit(
        config={"max_positions": 5, "initial_capital": 100000},
    )
    engine = Engine(
        strategy=strategy, provider=provider, db_path=str(db_path),
        initial_capital=100000,
    )
    engine.run("20240603", "20240612")

    from btcore import database
    conn = database.connect_result_db(str(db_path), read_only=True)
    try:
        buy = conn.execute(
            "SELECT shares FROM trade_log WHERE side='BUY' AND symbol='920469.BJ'"
        ).fetchone()
        stk = conn.execute(
            "SELECT shares, trigger FROM trade_log WHERE side='STK_DIV'"
        ).fetchone()
    finally:
        conn.close()
    assert buy is not None, "买入未成交，测试前提失败"
    assert stk is not None, "trade_log 缺少 STK_DIV 行"
    assert stk[0] == int(buy[0] * 1.3)
    assert stk[1] == "CORPORATE"


# ── INV-01: 同日送转+现金分红 ──


def test_combined_stk_cash_div_uses_pre_split_shares():
    """现金按送转前股数计息，scale 按组合除权公式（fixture 920089.BJ）。"""
    backend = MockDataBackend()
    div = backend.get_dividends_on_date("20240613")["920089.BJ"]
    assert div["stk_div"] == pytest.approx(0.4)  # 10 送 4
    assert div["cash_div"] == pytest.approx(0.12)  # 10 派 1.2

    holding = make_holding(symbol="920089.BJ", shares=74600,
                           entry_date="20240602",
                           entry_price=13.25, last_price=13.25)
    account = make_account(cash=0.0, holdings={"920089.BJ": holding})
    provider = FakeProvider()
    provider._dividends = {"20240613": {"920089.BJ": div}}

    log = []
    adjust(account, "20240613", {"920089.BJ": {"pre_close": 9.38}},
           provider, log)

    assert holding.shares == 104440  # int(74600 * 1.4)
    cash_log = next(e for e in log if e["type"] == "cash_div")
    assert cash_log["gross"] == pytest.approx(0.12 * 74600)  # 8952
    assert cash_log["tax_rate"] == 0.20
    assert cash_log["net"] == pytest.approx(7161.60)  # 非 10026.24
    assert account.cash == pytest.approx(7161.60)

    # 组合除权公式：scale = pre_close / (pre_close*(1+stk) + cash)
    expected_scale = 9.38 / (9.38 * 1.4 + 0.12)
    assert holding.entry_price == pytest.approx(13.25 * expected_scale)
    assert holding.last_price == pytest.approx(13.25 * expected_scale)

    trades = derived_trades(log)
    div_trade = next(t for t in trades if t.side == "DIV")
    assert div_trade.net_amount == pytest.approx(7161.60)
    stk_trade = next(t for t in trades if t.side == "STK_DIV")
    assert stk_trade.shares == 104440


def test_engine_combined_event_div_trade_log():
    """端到端: 引擎在 fixture 组合事件日落的 DIV net_amount 正确。"""
    from btcore.database import init_backtest_db
    from btcore.engine import Engine
    from btcore.provider import DataProvider
    from btcore.strategy import Strategy

    class HoldOnly(Strategy):
        def on_start(self, provider, first_date, end_date=None):
            pass

        def select(self, bars, snapshot, provider) -> dict:
            return {"buy": [], "sell": []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    provider = DataProvider(MockDataBackend())
    engine = Engine(HoldOnly(config={}), provider, initial_capital=1_000_000,
                    db_path=":memory:")
    engine.prepare("20240603", "20240614")
    engine.account.cash = 0.0
    engine.account.holdings["920089.BJ"] = make_holding(
        symbol="920089.BJ", shares=74600, entry_date="20240602",
        entry_price=13.25, last_price=13.25)

    conn = init_backtest_db(":memory:")
    try:
        engine.step("20240613", engine.bars_by_date["20240613"], conn)
        div_net = conn.execute(
            "SELECT net_amount FROM trade_log WHERE side='DIV'"
        ).fetchone()
        stk_shares = conn.execute(
            "SELECT shares FROM trade_log WHERE side='STK_DIV'"
        ).fetchone()
    finally:
        conn.close()

    assert div_net is not None, "trade_log 缺少 DIV 行"
    assert div_net[0] == pytest.approx(7161.60)
    assert stk_shares is not None, "trade_log 缺少 STK_DIV 行"
    assert stk_shares[0] == 104440
    # INV1 账户恒等式在组合事件日后仍成立（_settle 估值后）
    acct = engine.account
    holdings_mv = sum(h.shares * h.last_price for h in acct.holdings.values())
    assert acct.total_value == pytest.approx(acct.cash + holdings_mv)

