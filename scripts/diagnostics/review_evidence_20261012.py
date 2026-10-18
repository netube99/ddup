"""本次审查复现与独立结果重建；不是引擎实现或生产命令。

运行：.venv/bin/python scripts/diagnostics/review_evidence_20261012.py [--result /tmp/run.duckdb]
"""

import argparse
import json
import math
import sys
from datetime import date
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def reproduce() -> dict:
    from btcore.backend import DataBackend
    from btcore.costs import make_costs_fn
    from btcore.engine import Engine
    from btcore.match.manual import manual_buy
    from btcore.provider import DataProvider
    from btcore.slippage import apply_slippage
    from btcore.strategy import Strategy
    from btcore.types import Account

    symbol = "000001.SZ"
    bar = dict(open=10.99, close=10.99, high=11.0, low=10.8, pre_close=10.0,
               vol=10000.0, up_limit=11.0, down_limit=9.0, trade_date="20240603")
    account = Account(cash=10000.0, initial_capital=10000.0, slippage_ticks=2)
    trades = manual_buy(account, {symbol: bar}, [symbol], 1,
                        lambda *args: (11.0, 9.0), make_costs_fn({}), apply_slippage)

    class Backend(DataBackend):
        def query_bars(self, symbols, start, end, columns=None):
            rows = [dict(trade_date=d, symbol=symbol, open=10.0, close=10.0,
                         high=10.0, low=10.0, pre_close=10.0, adj_factor=1.0,
                         vol=10000.0, up_limit=11.0, down_limit=9.0)
                    for d in ["20240531", "20240603", "20240605"] if start <= d <= end]
            return pd.DataFrame(rows).set_index(["trade_date", "symbol"])

        def get_calendar(self, start, end):
            return [d for d in ["20240531", "20240603", "20240604", "20240605"]
                    if start <= d <= end]

        def get_dividends_on_date(self, date_str):
            return {symbol: {"cash_div": 0.5, "stk_div": 0.0}} if (
                date_str == "20240604"
            ) else {}

    class ProbeStrategy(Strategy):
        def __init__(self, sell=False):
            super().__init__({"initial_capital": 10000.0, "max_positions": 10,
                              "slippage_ticks": 0})
            self.sell = sell

        def select(self, bars, snapshot, provider):
            d = provider.get_as_of()
            return {"buy": [symbol] if d == "20240531" else [],
                    "sell": [symbol] if self.sell and d == "20240603" else []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days):
            return []

    hold = Engine(ProbeStrategy(), DataProvider(Backend()), db_path=":memory:")
    result = hold.run("20240603", "20240605")
    bought = int(result["trade_log"].query("side == 'BUY'")["shares"].sum())
    expected_dividend = bought * 0.5 * 0.8  # 持有不足 30 日：独立公式
    actual_dividend = float(result["trade_log"].query("side == 'DIV'")["net_amount"].sum())
    sell = Engine(ProbeStrategy(sell=True), DataProvider(Backend()), db_path=":memory:")
    sell_result = sell.run("20240603", "20240605")
    return {
        "F-MAT-02": {"actual_price": trades[0].price if trades else None, "up_limit": 11.0,
                     "order_rejected": not trades,
                     "within_limit": all(t.price <= 11.0 for t in trades)},
        "F-ENG-02": {"shares": bought, "expected_dividend": expected_dividend,
                     "actual_dividend": actual_dividend,
                     "cash_delta": actual_dividend - expected_dividend,
                     "settled_dates": result["account_daily"]["date"].tolist(),
                     "stale_sell_dates": sell_result["trade_log"].query(
                         "side == 'SELL'")["date"].tolist(),
                     "expected_stale_sell_dates": []},
    }


def shadow(result_path: str) -> dict:
    """仅使用结果表与原始行情独立重建；不调用 btcore 计算函数。"""
    conn = duckdb.connect(result_path, read_only=True)
    try:
        run = conn.execute("SELECT initial_capital, config_json, stats_json FROM runs "
                           "WHERE run_id=1").fetchone()
        events = conn.execute("SELECT * FROM trade_log WHERE run_id=1 ORDER BY date, "
                              "CASE side WHEN 'DIV' THEN 0 WHEN 'STK_DIV' THEN 1 ELSE 2 END, id"
                              ).df()
        daily = conn.execute("SELECT * FROM account_daily WHERE run_id=1 ORDER BY date").df()
    finally:
        conn.close()
    capital, config, stats = run[0], json.loads(run[1]), json.loads(run[2])
    market_path = "/home/netube/aiwork/tushare_db/data/market.duckdb"
    market = duckdb.connect(market_path, read_only=True)
    try:
        prices = market.execute(
            "SELECT trade_date, ts_code, open, close FROM stk_factor_pro "
            "WHERE trade_date BETWEEN ? AND ? AND ts_code IN (SELECT unnest(?))",
            [daily.iloc[0]["date"], daily.iloc[-1]["date"], events["symbol"].unique().tolist()],
        ).fetchall()
        dividends = market.execute(
            "SELECT ex_date, ts_code, cash_div FROM dividend WHERE div_proc='实施' "
            "AND ex_date BETWEEN ? AND ? AND ts_code IN (SELECT unnest(?))",
            [daily.iloc[0]["date"], daily.iloc[-1]["date"], events["symbol"].unique().tolist()],
        ).fetchall()
    finally:
        market.close()
    closes = {(d, s): float(p) for d, s, _, p in prices if p is not None}
    opens = {(d, s): float(p) for d, s, p, _ in prices if p is not None}
    divs = {}
    for d, s, amount in dividends:
        divs.setdefault((d, s), set()).add(amount)
    cash, shares, last, entries = capital, {}, {}, {}
    cash_error, value_error, fee_error, net_error = [], [], [], []
    dividend_error, price_error = [], []
    for row in daily.to_dict("records"):
        for ev in events[events["date"] == row["date"]].to_dict("records"):
            s, side = ev["symbol"], ev["side"]
            cash += ev["net_amount"]
            if side == "STK_DIV":
                shares[s] = int(ev["shares"])
                continue
            if side == "DIV":
                raw = divs[(row["date"], s)]
                assert len(raw) == 1, "本窗口仅核验单一现金分红事件"
                days = (date.fromisoformat(row["date"]) - date.fromisoformat(entries[s])).days
                tax = 0.2 if days <= 30 else (0.1 if days <= 365 else 0.0)
                expected = shares[s] * next(iter(raw)) * (1.0 - tax)
                dividend_error.append(abs(expected - ev["net_amount"]))
                continue
            value = ev["price"] * ev["shares"]
            commission = max(value * config.get("commission_rate", 0.00015),
                             config.get("min_commission", 5.0))
            transfer = value * config.get("transfer_fee_rate", 0.00001)
            stamp = value * config.get("stamp_tax_rate", 0.0005) if side == "SELL" else 0.0
            fee_error.extend([abs(commission - ev["commission"]),
                              abs(transfer - ev["transfer_fee"]), abs(stamp - ev["stamp_tax"])])
            expected_net = -(value + commission + transfer) if side == "BUY" else (
                value - commission - transfer - stamp)
            net_error.append(abs(expected_net - ev["net_amount"]))
            shares[s] = shares.get(s, 0) + (1 if side == "BUY" else -1) * int(ev["shares"])
            assert shares[s] >= 0
            if side == "BUY":
                entries.setdefault(s, row["date"])
            if ev["trigger"] == "MANUAL":
                direction = 1 if side == "BUY" else -1
                expected_price = round(opens[(row["date"], s)] + direction * 0.01 *
                                       config.get("slippage_ticks", 0), 2)
                price_error.append(abs(expected_price - ev["price"]))
            last[s] = ev["price"]
        for s, n in shares.items():
            if n and (row["date"], s) in closes:
                last[s] = closes[row["date"], s]
        value = cash + sum(n * last[s] for s, n in shares.items() if n)
        cash_error.append(abs(cash - row["cash"]))
        value_error.append(abs(value - row["total_value"]))
        assert cash >= -1e-6
        assert sum(n > 0 for n in shares.values()) == row["n_holdings"]
    return_error = abs(value / capital - 1.0 - stats["total_return"])
    values = [capital, *daily["total_value"].tolist()]
    peak, drawdown = capital, 0.0
    for v in values:
        peak = max(peak, v)
        drawdown = max(drawdown, 1.0 - v / peak)
    out = {"days": len(daily), "events": len(events),
           "max_cash_error": max(cash_error), "max_value_error": max(value_error),
           "max_fee_error": max(fee_error, default=0.0),
           "max_net_error": max(net_error, default=0.0), "return_error": return_error,
           "max_dividend_error": max(dividend_error, default=0.0),
           "dividend_events": len(dividend_error),
           "max_manual_price_error": max(price_error, default=0.0),
           "drawdown_error": abs(drawdown - stats["max_drawdown"])}
    assert all(math.isfinite(out[k]) and out[k] < 1e-6 for k in out if k.endswith("error"))
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--result")
    args = parser.parse_args()
    output = {"reproduction": reproduce()}
    if args.result:
        output["real_window_shadow"] = shadow(args.result)
    print(json.dumps(output, indent=2, ensure_ascii=False))
