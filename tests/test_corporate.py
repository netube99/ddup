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
