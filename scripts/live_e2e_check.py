"""实盘 CLI 全流程核查（真实行情库）：init → 每日 sync → 每日 signal。

以回测 result.db 为 ground truth 连续模拟 19 个交易日：
  - signal(D) 的操作单 open_sells/open_buys 必须等于回测在 D 的次一交易日实际成交
  - sync 的 statement 来自回测账本（cash/持仓/当日成交全量），必须 ok
  - 中途插入一次错报持仓 → 必须回滚拒绝，随后正确 statement 必须通过
  - 中途建账（OPENING 播种）与全程回放等价

用法: python scripts/live_e2e_check.py [--bt-db 回测库] [--ledger 账本] [--yaml 策略]
依赖真实行情库（adapters.tushare._DEFAULT_DB_PATH）；需先跑出含成交的回测。
"""
import argparse
import json
import os
import sqlite3
import subprocess
import sys

import yaml

_p = argparse.ArgumentParser()
_p.add_argument("--bt-db", default="results/live_migration/smoke_bt2.db")
_p.add_argument("--ledger", default="live/e2e_check.db")
_p.add_argument("--yaml", default="strategies/selected/trend_guard_bw_300/config.yaml")
_p.add_argument("--start", default=None, help="建账日（缺省=回测首个交易日）")
_p.add_argument("--days", default="20260105,20260204", help="模拟窗口 起,止")
_args = _p.parse_args()

BT = _args.bt_db
LEDGER = _args.ledger
MARKET = None
YAML = _args.yaml
PY = ".venv/bin/python"
BT_START = _args.start
WINDOW = _args.days.split(",")

conn = sqlite3.connect(BT)
rid = conn.execute("SELECT MAX(run_id) FROM trade_log").fetchone()[0]


def _market_path():
    global MARKET
    if MARKET is None:
        from adapters.tushare import _DEFAULT_DB_PATH
        MARKET = _DEFAULT_DB_PATH
    return MARKET


def cal_next(d):
    c = sqlite3.connect(_market_path())
    nxt = c.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date>?"
        " ORDER BY cal_date LIMIT 1", (d,)).fetchone()[0]
    c.close()
    return nxt


def trades_on(d):
    return conn.execute(
        "SELECT symbol, side, trigger, price, shares, commission, stamp_tax,"
        " transfer_fee FROM trade_log WHERE run_id=? AND date=? AND side IN"
        " ('BUY','SELL') ORDER BY id", (rid, d)).fetchall()


def account_state(d):
    """回测 ground truth：date d 收盘后的 cash + 持仓（含入场价/日）。"""
    cash = conn.execute(
        "SELECT cash FROM account_daily WHERE run_id=? AND date=?",
        (rid, d)).fetchone()[0]
    buys = conn.execute(
        "SELECT date, symbol, price, shares FROM trade_log WHERE run_id=?"
        " AND side='BUY' AND date<=? ORDER BY id", (rid, d)).fetchall()
    sells = conn.execute(
        "SELECT symbol, shares FROM trade_log WHERE run_id=? AND side='SELL'"
        " AND date<=? ORDER BY id", (rid, d)).fetchall()
    stkdiv = conn.execute(
        "SELECT symbol, shares FROM trade_log WHERE run_id=? AND"
        " side='STK_DIV' AND date<=? ORDER BY id", (rid, d)).fetchall()
    pos, entry = {}, {}
    for bd, s, p, sh in buys:
        pos[s] = pos.get(s, 0) + sh
        if s not in entry:
            entry[s] = {"entry_date": bd, "price": p, "shares": sh}
        else:
            e = entry[s]
            e["price"] = (e["price"] * e["shares"] + p * sh) / (e["shares"] + sh)
            e["shares"] += sh
    for s, sh in sells:
        pos[s] = pos.get(s, 0) - sh
    for s, sh in stkdiv:
        pos[s] = pos.get(s, 0) + sh
    holdings = {s: v for s, v in pos.items() if v > 0}
    return cash, holdings, entry


def cli(*args):
    p = subprocess.run([PY, "scripts/live.py", *args], capture_output=True,
                       text=True, cwd=".")
    try:
        return json.loads(p.stdout)
    except json.JSONDecodeError:
        print("NON-JSON OUTPUT:", p.stdout[:2000], p.stderr[:2000],
              file=sys.stderr)
        return None


def make_sync(date, cash, holdings, fills):
    return {"date": date, "cash": round(cash, 2),
            "holdings": [{"symbol": s, "shares": v}
                         for s, v in holdings.items()],
            "fills": fills}


def fills_from(trades, date):
    return [{"date": date, "symbol": s, "side": side, "price": p,
             "shares": sh, "commission": comm, "stamp_tax": tax,
             "transfer_fee": fee, "reason": trig}
            for s, side, trig, p, sh, comm, tax, fee in trades]


def main():
    if os.path.exists(LEDGER):
        os.remove(LEDGER)
    if os.path.exists("live/test_b.db"):
        os.remove("live/test_b.db")

    # ── Day 0: init（空仓；起始日 = 回测首个交易日）──
    if _args.start is None:
        bt_start = conn.execute(
            "SELECT MIN(date) FROM account_daily WHERE run_id=?", (rid,)
        ).fetchone()[0]
    cash0 = conn.execute(
        "SELECT initial_capital FROM runs WHERE run_id=?", (rid,)
    ).fetchone()[0]
    r = cli("init", LEDGER, "--date", bt_start, "--cash", str(round(cash0, 2)))
    assert r["ok"] and r["initial_capital"] > 0, r
    print(f"[init] ok: initial_capital={r['initial_capital']} start={bt_start}")

    # 交易日历 0105 → 0204（连续 19 个交易日，每日 sync + signal）
    c = sqlite3.connect(_market_path())
    days = [r[0] for r in c.execute(
        "SELECT cal_date FROM trade_cal WHERE is_open=1 AND cal_date BETWEEN"
        " '20260105' AND '20260204' ORDER BY cal_date")]
    c.close()
    assert len(days) == 23, days

    for d in days:
        nxt = cal_next(d)
        cash, holdings, _ = account_state(d)
        day_trades = trades_on(d)

        # 1) sync：每日全量 statement（含当日成交）
        sfile = f"/tmp/live_sync_{d}.yaml"
        with open(sfile, "w") as f:
            yaml.safe_dump(make_sync(d, cash, holdings,
                                     fills_from(day_trades, d)), f,
                           allow_unicode=True, sort_keys=False)
        r = cli("sync", LEDGER, sfile)
        assert r and r["ok"], f"sync {d} failed: {r}"
        assert abs(r["cash_adjust"]) < 0.02, f"unexpected adjust {d}: {r}"

        # 2) signal：操作单必须等于回测次日实际成交
        r = cli("signal", LEDGER, YAML, "--date", d)
        assert r, f"signal {d} failed"
        assert r["signal_date"] == d and r["trade_date"] == nxt, r
        nxt_trades = trades_on(nxt)
        # 开盘手动单（含 TB 盘前评估）= pending sell/buy 名单
        intraday = {"TAKE_PROFIT", "TRAILING_TP", "STOP_LOSS"}
        nxt_manual_sells = {t[0] for t in nxt_trades
                            if t[1] == "SELL" and t[2] not in intraday}
        nxt_manual_buys = {t[0] for t in nxt_trades if t[1] == "BUY"}
        nxt_cond_sells = [t for t in nxt_trades
                          if t[1] == "SELL" and t[2] in intraday]
        sheet_sells = {s["symbol"] for s in r["open_sells"]}
        sheet_buys = {b["symbol"] for b in r["open_buys"]}
        assert sheet_sells == nxt_manual_sells, (
            f"{d}: opsheet sells {sheet_sells} != backtest {nxt_manual_sells}")
        assert sheet_buys == nxt_manual_buys, (
            f"{d}: opsheet buys {sheet_buys} != backtest {nxt_manual_buys}")
        # 触发归因：手动卖单 reason 必须与回测 trigger 一一对应
        reason_map = {s["symbol"]: s["reason"] for s in r["open_sells"]}
        for sym, _, trig, *_ in nxt_trades:
            if sym in reason_map:
                assert reason_map[sym] == trig, (
                    f"{d}: {sym} reason {reason_map[sym]} != backtest {trig}")
        # 盘中条件单：必须在 broker_conditions 监控表里有同类型触发价
        cond_map = {x["symbol"]: {o["type"] for o in x["orders"]}
                    for x in r["broker_conditions"]}
        for sym, _, trig, *_ in nxt_cond_sells:
            assert trig in cond_map.get(sym, set()), (
                f"{d}: {sym} 盘中 {trig} 不在监控表 {cond_map.get(sym)}")
        tag = ""
        if day_trades:
            tag = f" fills={len(day_trades)}"
        if nxt_trades or day_trades or d in ("20260105", "20260120"):
            print(f"[{d}] ok{tag}: sells={sorted(sheet_sells)}"
                  f" buys={sorted(sheet_buys)}"
                  f" cond_orders={sum(len(x['orders']) for x in r['broker_conditions'])}"
                  f" -> {nxt}")

    # ── 3) 错报持仓 → 拒绝；随后正确 statement → 通过（验证回滚干净）──
    d = days[-1]
    cash, holdings, _ = account_state(d)
    bad = dict(holdings)
    dropped = next(iter(bad))
    del bad[dropped]
    sfile = "/tmp/live_sync_bad.yaml"
    with open(sfile, "w") as f:
        yaml.safe_dump(make_sync(d, cash, bad, fills_from(trades_on(d), d)), f,
                       allow_unicode=True, sort_keys=False)
    r = cli("sync", LEDGER, sfile)
    assert r and not r["ok"], "bad sync should be rejected"
    assert dropped in r["holding_diffs"], r
    print(f"[reject] ok: 缺 {dropped} → 拒绝, diffs={r['holding_diffs']}")
    with open(sfile, "w") as f:
        yaml.safe_dump(make_sync(d, cash, holdings, fills_from(trades_on(d), d)),
                       f, allow_unicode=True, sort_keys=False)
    r = cli("sync", LEDGER, sfile)
    assert r and r["ok"], f"recovery sync failed: {r}"
    print("[recover] ok: 正确 statement 通过，回滚未污染账本")

    # ── 4) status ──
    r = cli("status", LEDGER)
    assert r["last_day"] and r["holdings"], r
    assert r["last_day"]["date"] == days[-1], r
    print(f"[status] ok: last={r['last_day']['date']}"
          f" cash={r['last_day']['cash']:.2f}"
          f" holdings={len(r['holdings'])}")

    # ── 5) 中途建账（OPENING 播种）等价性：B 在 0108 建账 vs A 回放 ──
    lb = "live/test_b.db"
    d = days[3]
    cash, holdings, entry = account_state(d)
    pos_file = "/tmp/live_e2e_positions.yaml"
    with open(pos_file, "w") as f:
        yaml.safe_dump({"positions": [
            {"symbol": s, "shares": v, "entry_date": entry[s]["entry_date"],
             "entry_price": round(entry[s]["price"], 4)}
            for s, v in holdings.items()]}, f, allow_unicode=True,
            sort_keys=False)
    r = cli("init", lb, "--date", d, "--cash", str(round(cash, 2)),
            "--positions", pos_file)
    assert r["ok"], r
    rb = cli("signal", lb, YAML, "--date", d)
    ra = cli("signal", LEDGER, YAML, "--date", d)
    assert rb["account"] == ra["account"], (
        f"seed 不等价:\nB={rb['account']}\nA={ra['account']}")
    assert rb["open_sells"] == ra["open_sells"]
    assert rb["open_buys"] == ra["open_buys"]
    print("[seed parity] ok: B(中途建账+播种) == A(全程回放) 账户/操作单一致")

    conn.close()
    print("\n=== 全流程通过 ===")


if __name__ == "__main__":
    main()
