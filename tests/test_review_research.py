"""TDD 审查回归 — research/ + scripts/ + strategies/ 范围发现的最小锁定测试。

每条测试对应一份审查发现（F-RSR-NN），先红后绿：
  - 小单买入阈值应随 config 费率推导（最低佣金触发边界 = min_commission/rate）
  - index_universe 契约类型是 list[str]（字符串会静默退化为全市场）
  - replay --symbol 无匹配快照时应按文档以退出码 1 报错
"""

import json
import sqlite3

import pandas as pd

from btcore.strategy_loader import load_strategy
from research.cross_validate import validate_trades
from research.replay import run_replay

TRADE_COLS = ["date", "symbol", "side", "trigger", "price", "shares", "turnover",
              "commission", "stamp_tax", "transfer_fee", "slippage_amount",
              "net_amount", "reason"]


def _buy(turnover: float) -> list:
    shares = 1000
    price = turnover / shares
    return ["20240603", "000001.SZ", "BUY", "MANUAL", price, shares, turnover,
            5.0, 0.0, 0.0, 0.0, -turnover - 5.0, ""]


def test_small_buy_threshold_tracks_min_commission_boundary():
    """turnover 30000（默认费率 4.5 < 5 元最低佣金）必须计入小单。

    合同（docs/cli_and_research.md §2.5）：小单买入 = 买入触发最低佣金。
    触发边界 = min_commission / commission_rate = 33333（默认费率），
    硬编码 25000 会漏计 [25000, 33333) 区间 → >50% 小单 false-pass。
    """
    trades = pd.DataFrame([_buy(30000.0)], columns=TRADE_COLS)
    issues, _ = validate_trades(trades, {"initial_capital": 100000})
    assert any("TOO_MANY_SMALL_TRADES" in i for i in issues)


def test_small_buy_threshold_no_false_positive_on_high_rate():
    """commission_rate=0.0003 时边界=16667：turnover 20000 不触发最低佣金，
    不得误报小单（硬编码 25000 会把未触发最低佣金的买单计入）。"""
    trades = pd.DataFrame([_buy(20000.0)], columns=TRADE_COLS)
    config = {"initial_capital": 100000, "commission_rate": 0.0003,
              "min_commission": 5.0}
    issues, _ = validate_trades(trades, config)
    assert not any("TOO_MANY_SMALL_TRADES" in i for i in issues)


def test_re_attack_500_index_universe_is_code_list():
    """filter_rules.index_universe 契约类型是 list[str]（strategy_guide §4.4）。

    字符串 "000905.SH" 会被 resolve_index_snapshots 的 list(codes) 拆成
    单字符列表 → 成分查询为空 → 白名单静默不生效（交易域退化为全市场），
    且 engine 基准推导 len==1 判断失效（回退 000300.SH）。
    """
    strategy = load_strategy("strategies/exploring/re_attack_500/config.yaml")
    iu = strategy.FILTER_RULES["index_universe"]
    assert isinstance(iu, list) and len(iu) >= 1
    assert all(isinstance(c, str) and "." in c for c in iu)


def _make_debug_db(tmp_path, symbols_in_snapshot):
    db = tmp_path / "replay.db"
    conn = sqlite3.connect(db)
    conn.execute("""CREATE TABLE debug_snapshots (
        run_id INTEGER, date TEXT, snapshot_json TEXT,
        PRIMARY KEY (run_id, date))""")
    snap = {
        "date": "20240603",
        "account": {"cash": 100000.0, "total_value": 100000.0, "n_holdings": 0},
        "pending": {"buy": [], "sell": []},
        "holdings_detail": {},
        "bars_subset": {s: {"close": 10.0} for s in symbols_in_snapshot},
    }
    conn.execute("INSERT INTO debug_snapshots VALUES (1, '20240603', ?)",
                 (json.dumps(snap),))
    conn.execute("""CREATE TABLE runs (run_id INTEGER PRIMARY KEY)""")
    conn.execute("INSERT INTO runs VALUES (1)")
    conn.commit()
    conn.close()
    return str(db)


def test_replay_symbol_no_match_exits_nonzero(tmp_path, capsys):
    """--symbol 过滤后无任何匹配快照 → 文档契约（cli_and_research §2.7）
    要求打印错误并退出码 1，而非静默空输出 + 0。"""
    db = _make_debug_db(tmp_path, ["000001.SZ"])
    rc = run_replay(db, 1, symbol="999999.SZ")
    assert rc == 1


def test_replay_symbol_match_still_prints(tmp_path, capsys):
    """匹配的 symbol 正常输出（防回归：修复不得误伤命中路径）。"""
    db = _make_debug_db(tmp_path, ["000001.SZ"])
    rc = run_replay(db, 1, symbol="000001.SZ")
    assert rc == 0
    out = capsys.readouterr().out
    assert "=== 20240603 ===" in out
