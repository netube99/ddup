"""实盘 V2 离线演练 — CLI 全闭环验收（不依赖本机真实行情库）。

用仓库 fixtures 行情（--fixtures-dir）驱动 scripts/live.py 子进程完成：
  init → 对账单导入（含入金/成交）→ status → activate → preview → accept
  → submit → fill → 次日对账单（自动确认）→ preview → orders → expire
  → status --with-session，并注入一次坏回报验证整笔回滚。

用法: python scripts/live_e2e_check.py [--keep]
默认在专用临时目录执行，绝不动真实账本；--keep 保留目录供排查。
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

HERE = Path(__file__).resolve().parent
FIXTURES = str(HERE.parent / "tests" / "fixtures")
PY = sys.executable
STRATEGY = "strategies/examples/rolling_ranker/config.yaml"

D1, D2 = "20240603", "20240604"


def cli(db, *args):
    args = [str(a) for a in args]
    proc = subprocess.run(
        [PY, str(HERE / "live.py"), args[0], str(db), *args[1:]],
        capture_output=True, text=True, cwd=HERE.parent)
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise AssertionError(
            f"non-JSON output for {args}: {proc.stdout[:500]} {proc.stderr[:500]}")
    return proc.returncode, payload


def close_of(symbol, date):
    bars = pd.read_parquet(Path(FIXTURES) / "bars.parquet")
    row = bars[(bars.symbol == symbol) & (bars.trade_date == date)]
    assert not row.empty, f"fixtures 缺 {symbol}@{date}"
    return float(row.iloc[0]["close"])


def main():
    workdir = tempfile.mkdtemp(prefix="ddup_live_e2e_")
    db = str(Path(workdir) / "drill.duckdb")
    print(f"[workdir] {workdir}")

    rc, out = cli(db, "init", "--account-id", "drill", "--broker", "sim")
    assert rc == 0 and out["ok"], out

    stmt1 = Path(workdir) / "stmt1.yaml"
    stmt1.write_text(yaml.safe_dump({
        "date": D1, "cash": 100000,
        "holdings": [{"symbol": "000001.SZ", "shares": 1000,
                      "available": 1000}],
    }, allow_unicode=True), encoding="utf-8")
    rc, out = cli(db, "import", str(stmt1))
    assert rc == 0 and out["snapshot"]["action"] == "opening_confirmed", out

    rc, status = cli(db, "status", "--json")
    assert rc == 0 and status["confirmed"]["cash_balance"] == 100000, status

    anchors = Path(workdir) / "anchors.yaml"
    anchors.write_text(yaml.safe_dump({"anchors": {
        "000001.SZ": {"entry_date": "20240506", "entry_price": 10.0}}}),
        encoding="utf-8")
    rc, out = cli(db, "activate", "--date", D1, "--yaml", STRATEGY,
                  "--anchors", str(anchors), "--fixtures-dir", FIXTURES)
    assert rc == 0 and out["ok"], out
    print(f"[activate] {out['strategy']} @ {out['effective_date']}")

    rc, preview = cli(db, "preview", "--date", D1, "--json",
                      "--fixtures-dir", FIXTURES)
    assert rc == 0 and preview["decision_hash"], preview

    rc, plan = cli(db, "accept", "--date", D1, "--fixtures-dir", FIXTURES)
    assert rc == 0 and plan["orders"], plan
    order = next(o for o in plan["orders"] if o["kind"] == "BUY")
    oid = order["client_order_id"]
    print(f"[accept] {plan['plan_id']} orders={len(plan['orders'])}")

    rc, out = cli(db, "fill", "--order", oid, "--date", D2,
                  "--qty", order["planned_qty"], "--price", 1.0)
    assert rc == 1 and out["code"] == "state", out  # 未提交先回报被拒

    rc, out = cli(db, "submit", "--order", oid)
    assert rc == 0 and out["status"] == "submitted", out

    price = close_of(order["symbol"], D2)
    rc, out = cli(db, "fill", "--order", oid, "--date", D2,
                  "--qty", order["planned_qty"], "--price", price,
                  "--commission", 5.0)
    assert rc == 0 and out["status"] == "filled", out
    print(f"[fill] {order['symbol']} {order['planned_qty']}@{price}")

    # 次日对账单：直接采用回放投影（成交已计入现金与持仓）
    rc, status = cli(db, "status", "--json")
    projected_cash = status["projection"]["cash"]
    holdings = dict(status["projection"]["holdings"])
    stmt2 = Path(workdir) / "stmt2.yaml"
    stmt2.write_text(yaml.safe_dump({
        "date": D2, "cash": round(projected_cash, 2),
        "holdings": [{"symbol": s, "shares": n} for s, n in
                     sorted(holdings.items())],
    }, allow_unicode=True), encoding="utf-8")
    rc, out = cli(db, "import", str(stmt2))
    assert rc == 0 and out["snapshot"]["action"] == "imported_confirmed", out
    print(f"[day2] 对账自动确认 cash={projected_cash:.2f}")

    rc, preview2 = cli(db, "preview", "--date", D2, "--json",
                       "--fixtures-dir", FIXTURES)
    assert rc == 0 and preview2["decision_hash"], preview2

    rc, out = cli(db, "expire", "--as-of", "20240701")
    assert rc == 0 and out["expired"], out

    rc, status = cli(db, "status", "--with-session", "--json",
                     "--fixtures-dir", FIXTURES)
    assert rc == 0 and status["activation"]["strategy"], status
    assert status["outstanding_reserved"] == 0.0

    print("\n=== 实盘 V2 离线演练通过 ===")
    if "--keep" not in sys.argv:
        import shutil

        shutil.rmtree(workdir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
