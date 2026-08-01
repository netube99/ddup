from btcore.corporate import adjust
from tests.conftest import make_account, make_holding


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

    import sqlite3
    conn = sqlite3.connect(db_path)
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

