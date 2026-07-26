from dataclasses import dataclass

from btcore.database import (
    init_backtest_db,
    write_daily,
    write_run,
    write_run_stats,
    write_trade,
)
from btcore.stats import calculate_statistics
from research.report import (
    build_compare_table,
    generate_compare_report,
    generate_report,
    generate_report_from_db,
    load_runs,
)
from tests.test_stats import SAMPLE_TRADES, make_account_daily, make_trades


@dataclass
class FakeTrade:
    date: str = "20240603"
    symbol: str = "000001.SZ"
    side: str = "BUY"
    trigger: str = "MANUAL"
    price: float = 10.0
    shares: int = 100
    turnover: float = 1000.0
    commission: float = 1.5
    stamp_tax: float = 0.0
    transfer_fee: float = 0.01
    slippage_amount: float = 0.04
    net_amount: float = -1001.55
    reason: str = "MANUAL"


def make_result():
    adf = make_account_daily(
        [1_000_000.0, 1_010_000.0, 1_005_000.0, 1_020_000.0, 1_030_000.0],
        n_holdings=[0, 2, 2, 3, 3],
    )
    tdf = make_trades(SAMPLE_TRADES)
    return {
        "account_daily": adf,
        "trade_log": tdf,
        "statistics": calculate_statistics(adf, tdf),
    }


def _seed_db(db_path: str, with_stats: bool = True) -> tuple[int, int]:
    conn = init_backtest_db(db_path)
    run_ids = []
    for k, strategy in enumerate(("s1", "s2")):
        run_id = write_run(
            conn, created_at="2024-06-01", strategy=strategy,
            start_date="20240603", end_date="20240607",
            initial_capital=1_000_000.0, config_json="{}", status="completed",
        )
        values = [1_000_000.0 + 10_000.0 * (i + k) for i in range(5)]
        dates = [f"202406{3 + i:02d}" for i in range(5)]
        for d, v in zip(dates, values):
            write_daily(conn, run_id, d, v / 2, v, 0.0, 0.0, 1_000_000.0,
                        n_holdings=2)
        write_trade(conn, run_id, FakeTrade(date=dates[0]))
        if with_stats:
            write_run_stats(conn, run_id, {"total_return": 0.01 * (k + 1)})
        run_ids.append(run_id)
    conn.commit()
    conn.close()
    return tuple(run_ids)


def test_generate_report(tmp_path):
    out = tmp_path / "report.html"
    generate_report(make_result(), str(out))
    content = out.read_text(encoding="utf-8")
    for marker in ("核心指标", "净值曲线", "回撤曲线", "交易磨损", "持仓管理复杂度",
                   "往返交易汇总", "个股盈亏贡献", "成交明细", "<svg", "AAA"):
        assert marker in content


def test_generate_report_from_db(tmp_path):
    db_path = str(tmp_path / "r.db")
    _seed_db(db_path)
    out = tmp_path / "from_db.html"
    generate_report_from_db(db_path, str(out))
    content = out.read_text(encoding="utf-8")
    assert "核心指标" in content
    assert "run 2" in content  # 缺省取最新 run


def test_load_runs_recompute_when_stats_missing(tmp_path):
    db_path = str(tmp_path / "r.db")
    _seed_db(db_path, with_stats=False)
    runs = load_runs(db_path)
    assert len(runs) == 2
    for run in runs:
        assert "total_return" in run["statistics"]
        assert "trading_friction" in run["statistics"]


def test_compare(tmp_path):
    db_path = str(tmp_path / "r.db")
    _seed_db(db_path)
    runs = load_runs(db_path)
    header, rows = build_compare_table(runs)
    assert header[0] == "指标"
    assert len(header) == 3
    labels = [r[0] for r in rows]
    assert "总收益率" in labels
    assert "总交易成本" in labels

    out = tmp_path / "cmp.html"
    generate_compare_report(db_path, str(out))
    content = out.read_text(encoding="utf-8")
    assert "多 run 对比报告" in content
    assert "归一化净值对比" in content
    assert "<svg" in content
