"""cross_validate 测试 — 自定义 trigger 降级 / 键名对齐 / 成本口径从 config 读取。

用合成 DuckDB 结果库验证：DYNAMIC_STOP 等自定义 handler 的 trigger 不再误报
UNEXPECTED_TRIGGER（示例策略 condition_hunter/multi_model 同款），
统计指标键名与 btcore.stats 实际输出对齐，STK_DIV 行不破坏买卖计数。
"""

import json
import subprocess
import sys

import pytest

from btcore import database
from research.cross_validate import (
    _expected_triggers,
    _min_commission_overhead,
    validate_trades,
)
from tests.test_stats import make_trades


def make_db(tmp_path, stats_json=None, config_json=None, extra_trades=()):
    """构造最小结果库（runs + trade_log + account_daily）。"""
    db = tmp_path / "cv.duckdb"
    conn = database.init_backtest_db(str(db))
    conn.execute(
        "INSERT INTO runs (run_id, created_at, strategy, start_date, end_date,"
        " initial_capital, config_json, status, stats_json)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        [1, "2026", "demo", "20240603", "20240607", 100000,
         json.dumps(config_json or {"initial_capital": 100000}),
         "completed", json.dumps(stats_json or {})],
    )
    base = [
        (1, "20240603", "000001.SZ", "BUY", "MANUAL", 10.0, 10000, 100000.0,
         5.0, 0.0, 0.0, 0.0, -100005.0, ""),
        (1, "20240605", "000001.SZ", "SELL", "MANUAL", 11.0, 10000, 110000.0,
         5.0, 55.0, 0.0, 0.0, 109940.0, ""),
    ]
    for t in base + list(extra_trades):
        conn.execute(
            "INSERT INTO trade_log (run_id,date,symbol,side,trigger,price,shares,"
            "turnover,commission,stamp_tax,transfer_fee,slippage_amount,"
            "net_amount,reason) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", list(t))
    conn.execute(
        "INSERT INTO account_daily (run_id, date, cash, total_value, daily_pnl,"
        " initial_capital, n_holdings) VALUES (?,?,?,?,?,?,?)",
        [1, "20240603", 90000, 100000, 0, 100000, 1],
    )
    conn.close()
    return db


def test_expected_triggers_include_registry():
    """预期 trigger 集合 = 引擎固定集 ∪ 条件单注册表。

    自定义 handler（如示例策略的 DYNAMIC_STOP）只在策略进程注册，
    独立运行的 cross_validate 看不到——这正是"未知 trigger 降级 INFO"
    要覆盖的场景（见 test_custom_trigger_no_false_positive）。
    """
    trigs = _expected_triggers()
    assert {"MANUAL", "TARGET", "CORPORATE", "STOP_LOSS", "TAKE_PROFIT",
            "TRAILING_TP", "LIMIT_BUY", "BREAKOUT_BUY", "ML_EXIT"} <= trigs


def test_custom_trigger_no_false_positive():
    """DYNAMIC_STOP（示例策略同款）不再产生 UNEXPECTED_TRIGGER issue。"""
    trades = make_trades([
        ["20240603", "000001.SZ", "BUY", "MANUAL", 10.0, 1000, 10000.0,
         5.0, 0.0, 0.0, 0.0, -10005.0, ""],
        ["20240605", "000001.SZ", "SELL", "DYNAMIC_STOP", 11.0, 1000, 11000.0,
         5.0, 5.5, 0.0, 0.0, 10989.5, ""],
    ])
    issues, notes = validate_trades(trades, {"initial_capital": 100000})
    assert not any("UNEXPECTED_TRIGGER" in i for i in issues)
    assert any("非内置触发类型" in n for n in notes)


def test_stk_div_side_ignored_in_counts():
    """STK_DIV 行不进入买卖计数，也不产生问题。"""
    trades = make_trades([
        ["20240603", "000001.SZ", "BUY", "MANUAL", 13.4, 3000, 40200.0,
         6.03, 0.0, 0.0, 0.0, -40206.03, ""],
        ["20240604", "000001.SZ", "STK_DIV", "CORPORATE", 0.0, 4200, 0.0,
         0.0, 0.0, 0.0, 0.0, 0.0, "stk_div"],
        ["20240605", "000001.SZ", "SELL", "MANUAL", 7.1, 4200, 29820.0,
         5.0, 14.91, 0.0, 0.0, 29800.09, ""],
    ])
    issues, notes = validate_trades(trades, {"initial_capital": 100000})
    assert not issues
    n_buys = int((trades["side"] == "BUY").sum())
    n_sells = int((trades["side"] == "SELL").sum())
    assert n_buys == 1 and n_sells == 1


def test_min_commission_from_config():
    """成本口径从 config 读取（引擎费率可配置，不再硬编码 5 元）。"""
    assert _min_commission_overhead(100, 100_000, 5.0) == pytest.approx(0.005)
    assert _min_commission_overhead(100, 100_000, 1.0) == pytest.approx(0.001)


def test_main_exit_zero_for_custom_trigger(tmp_path):
    """主流程对自定义 trigger 的 run 退出码 = 0（不再误报问题数）。"""
    db = make_db(tmp_path, extra_trades=[
        (1, "20240606", "000001.SZ", "BUY", "LIMIT_BUY", 9.0, 3000, 27000.0,
         5.0, 0.0, 0.0, 0.0, -27005.0, ""),
        (1, "20240607", "000001.SZ", "SELL", "DYNAMIC_STOP", 9.5, 3000, 28500.0,
         5.0, 14.25, 0.0, 0.0, 28480.75, ""),
    ])
    r = subprocess.run(
        [sys.executable, "scripts/cross_validate.py", str(db)],
        capture_output=True, text=True,
    )
    assert r.returncode == 0
    assert "非内置触发类型" in r.stdout


def test_main_prints_real_stats_keys(tmp_path):
    """统计指标键名与 stats.py 实际输出对齐（年化/夏普/平均持有天数）。"""
    stats = {
        "total_return": 0.1, "annualized_return": 0.2, "max_drawdown": -0.05,
        "sharpe": 1.5, "calmar": 2.0, "win_rate": 0.55,
        "round_trip": {"summary": {"avg_holding_days": 3.5}},
    }
    db = make_db(tmp_path, stats_json=stats)
    r = subprocess.run(
        [sys.executable, "scripts/cross_validate.py", str(db)],
        capture_output=True, text=True,
    )
    assert "年化收益率: 0.2000" in r.stdout
    assert "夏普比率: 1.5000" in r.stdout
    assert "平均持有天数: 3.50" in r.stdout
