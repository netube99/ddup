"""dump_brinson_data.py 导出口径回归（F-CLI-01 / F-CLI-02）。

sw_returns 必须与消费方 research/attribution._load_sw_returns 同口径：
仅 index_member_all.l1_code 登记的 L1 行业代码；同名重复按 ts_code 升序
取首行，取值不依赖库文件物理扫描顺序。参数前置校验必须先于任何
parquet 写出（脚本 TOOL-01 自述契约）。
"""

import sys

import duckdb
import pandas as pd
import pytest

import scripts.dump_brinson_data as dump


def _make_market_db(path):
    """最小行情库：含同名多 ts_code 行与非 L1 行业行。"""
    conn = duckdb.connect(str(path))
    conn.execute(
        "CREATE TABLE index_member_all (ts_code VARCHAR, l1_name VARCHAR,"
        " l1_code VARCHAR)"
    )
    # 801712.SI / 801714.SI 登记为 L1；801713.SI、801750.SI 未登记
    conn.executemany(
        "INSERT INTO index_member_all VALUES (?, ?, ?)",
        [
            ("600036.SH", "银行", "801780.SI"),
            ("601100.SH", "水泥制造", "801712.SI"),
            ("601318.SH", "水泥制造", "801714.SI"),
        ],
    )
    conn.execute(
        "CREATE TABLE sw_daily (trade_date VARCHAR, ts_code VARCHAR,"
        " name VARCHAR, pct_change DOUBLE)"
    )
    # 物理插入顺序与 ts_code 升序相反：证明取值不依赖扫描顺序
    conn.executemany(
        "INSERT INTO sw_daily VALUES (?, ?, ?, ?)",
        [
            ("20240603", "801714.SI", "水泥制造", 0.9),   # L1，ts_code 更大
            ("20240603", "801712.SI", "水泥制造", 0.3),   # L1，ts_code 最小
            ("20240603", "801713.SI", "水泥制造", 0.6),   # 非 L1，应整行排除
            ("20240603", "801780.SI", "银行", 1.0),
            ("20240603", "801750.SI", "食品饮料Ⅱ", 2.0),  # 非 L1 行业
        ],
    )
    conn.execute(
        "CREATE TABLE index_weight (index_code VARCHAR, con_code VARCHAR,"
        " trade_date VARCHAR, weight DOUBLE)"
    )
    conn.execute(
        "INSERT INTO index_weight VALUES ('000300.SH', '600036.SH',"
        " '20240603', 1.0)"
    )
    conn.close()
    return str(path)


def _run(monkeypatch, argv):
    monkeypatch.setattr(sys, "argv", ["dump_brinson_data.py", *argv])
    return dump.main()


def test_sw_returns_l1_only_and_deterministic_dedup(tmp_path, monkeypatch):
    """F-CLI-01：只导 L1 行业列；同名重复取 ts_code 最小行。"""
    db = _make_market_db(tmp_path / "market.duckdb")
    out = tmp_path / "out"

    assert _run(monkeypatch, [db, "--out", str(out)]) == 0

    sw = pd.read_parquet(out / "sw_returns.parquet")
    # 非 L1 的『食品饮料Ⅱ』列不出现；801713.SI（未登记 L1）不参与同名去重
    assert set(sw.columns) == {"水泥制造", "银行"}
    assert sw.loc["20240603", "水泥制造"] == pytest.approx(0.003)
    assert sw.loc["20240603", "银行"] == pytest.approx(0.01)


def test_result_db_without_dates_writes_nothing(tmp_path, monkeypatch, capsys):
    """F-CLI-02：--result-db 缺日期必须在写出任何 parquet 前报错。

    修复前先写出 industry_map/sw_returns/benchmark_weights 三个文件后才
    parser.error，留下看似完成的导出目录。
    """
    db = _make_market_db(tmp_path / "market.duckdb")
    out = tmp_path / "out"

    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, [db, "--out", str(out), "--result-db",
                           str(tmp_path / "bt.duckdb")])
    assert exc.value.code == 2
    assert "--result-db" in capsys.readouterr().err
    assert not out.exists() or list(out.glob("*.parquet")) == []
