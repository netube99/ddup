"""Brinson 归因测试 — 合成数据单测 + 真库冒烟。"""

import sqlite3

import pandas as pd
import pytest

from research.attribution import (
    _aggregate_period,
    _compute_brinson_daily,
    _reconstruct_daily_holdings,
    brinson_attribute,
    brinson_attribute_from_files,
)

# ═══════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════


@pytest.fixture
def synth_trade_log():
    """合成 5 日交易数据: 银行+食品饮料各 1 只股票，每日买卖。"""
    return pd.DataFrame([
        {"id": 1, "date": "20240603", "symbol": "600036.SH", "side": "BUY",
         "shares": 10000, "price": 35.0},
        {"id": 2, "date": "20240603", "symbol": "600519.SH", "side": "BUY",
         "shares": 2000, "price": 1600.0},
        {"id": 3, "date": "20240604", "symbol": "600036.SH", "side": "BUY",
         "shares": 5000, "price": 35.5},
        {"id": 4, "date": "20240605", "symbol": "600036.SH", "side": "SELL",
         "shares": 5000, "price": 36.0},
        {"id": 5, "date": "20240606", "symbol": "600519.SH", "side": "SELL",
         "shares": 1000, "price": 1620.0},
    ])


@pytest.fixture
def synth_bars():
    """合成 5 日 bars: 两只股票 close 均按首日基准价每日 +2% 线性递增。"""
    dates = ["20240603", "20240604", "20240605", "20240606", "20240607"]
    records = []
    for i, d in enumerate(dates):
        records.append({
            "trade_date": d, "symbol": "600036.SH",
            "close": 35.0 * (1 + 0.02 * i), "pct_chg": 2.0,
        })
        records.append({
            "trade_date": d, "symbol": "600519.SH",
            "close": 1600.0 * (1 + 0.02 * i), "pct_chg": 5.0,
        })
    df = pd.DataFrame(records)
    df.set_index(["trade_date", "symbol"], inplace=True)
    df.sort_index(inplace=True)
    return df


@pytest.fixture
def synth_industry_map():
    return {"600036.SH": "银行", "600519.SH": "食品饮料"}


@pytest.fixture
def synth_sw_returns():
    """合成行业日收益: 银行 3%（超配的行业跑赢），食品 1%。"""
    dates = ["20240603", "20240604", "20240605", "20240606", "20240607"]
    records = []
    for d in dates:
        records.append({"date": d, "industry": "银行", "ret": 0.03})
        records.append({"date": d, "industry": "食品饮料", "ret": 0.01})
    df = pd.DataFrame(records)
    return df.pivot_table(index="date", columns="industry", values="ret")


@pytest.fixture
def synth_benchmark_weights():
    """合成基准行业权重: 银行 30%，食品 20%，其他 50%。"""
    dates = ["20240603", "20240604", "20240605", "20240606", "20240607"]
    records = []
    for d in dates:
        records.append({"date": d, "行业": "银行", "weight": 0.30})
        records.append({"date": d, "行业": "食品饮料", "weight": 0.20})
    df = pd.DataFrame(records)
    return df.pivot_table(index="date", columns="行业", values="weight")


# ═══════════════════════════════════════════
# 合成数据单测
# ═══════════════════════════════════════════


class TestReconstructHoldings:

    def test_carry_forward_no_trade_days(self, synth_bars, synth_industry_map):
        """无成交日持仓逐日结转：买入持有 5 天 → 5 行，权重恒 1.0。"""
        trade_log = pd.DataFrame([
            {"id": 1, "date": "20240603", "symbol": "600036.SH",
             "side": "BUY", "shares": 10000},
        ])
        df = _reconstruct_daily_holdings(trade_log, synth_bars, synth_industry_map)
        assert len(df) == len(synth_bars.index.get_level_values("trade_date").unique())
        # 无成交的中间日（20240604）持仓仍为银行 100%
        assert df.loc["20240604", "银行_weight"] == pytest.approx(1.0)
        assert df.loc["20240604", "portfolio_return"] == pytest.approx(0.02)

    def test_stk_div_updates_shares(self, synth_bars, synth_industry_map):
        """STK_DIV 行（shares=送转后总股数）覆盖重建股数，后续卖出不再超卖。"""
        trade_log = pd.DataFrame([
            {"id": 1, "date": "20240603", "symbol": "600036.SH",
             "side": "BUY", "shares": 100},
            {"id": 2, "date": "20240604", "symbol": "600036.SH",
             "side": "STK_DIV", "shares": 150},
            {"id": 3, "date": "20240605", "symbol": "600036.SH",
             "side": "SELL", "shares": 150},
        ])
        df = _reconstruct_daily_holdings(trade_log, synth_bars, synth_industry_map)
        # 送转日：按新总股数持仓，权重仍为 1.0
        assert df.loc["20240604", "银行_weight"] == pytest.approx(1.0)
        # 送转后全额卖出：20240605 空仓（权重列为其他日期并集产生的 NaN）
        assert df.loc["20240605", "portfolio_return"] == 0.0
        assert pd.isna(df.loc["20240605", "银行_weight"])
    def test_basic_reconstruction(self, synth_trade_log, synth_bars, synth_industry_map):
        df = _reconstruct_daily_holdings(synth_trade_log, synth_bars, synth_industry_map)
        assert not df.empty
        # 20240603: 买入银行和食品
        assert "银行_weight" in df.columns


class TestBrinsonDaily:
    def test_benchmark_weights_ffilled(self, synth_sw_returns):
        """基准权重快照（稀疏）ffill 到日频：快照间日期参与归因。"""
        dates = synth_sw_returns.index
        bw = pd.DataFrame({"银行": 0.3, "食品饮料": 0.2},
                          index=[dates[0], dates[-1]])
        records = []
        for d in dates:
            records.append({"date": d, "银行_weight": 0.3, "银行_return": 0.03,
                           "食品饮料_weight": 0.2, "食品饮料_return": 0.01,
                           "portfolio_return": 0.03})
        holdings_df = pd.DataFrame(records).set_index("date")
        daily_df = _compute_brinson_daily(holdings_df, bw, synth_sw_returns)
        assert len(daily_df) == len(dates)

    def test_benchmark_return_includes_unheld_industries(self, synth_sw_returns):
        """基准收益按全基准行业累加（含策略从未持有的行业）。"""
        dates = synth_sw_returns.index
        bw = pd.DataFrame({"银行": 0.3, "医药": 0.7}, index=dates)
        records = []
        for d in dates:
            records.append({"date": d, "银行_weight": 1.0, "银行_return": 0.03,
                           "portfolio_return": 0.03})
        holdings_df = pd.DataFrame(records).set_index("date")
        daily_df = _compute_brinson_daily(holdings_df, bw, synth_sw_returns)
        # 银行 3% + 医药 1%（synth_sw_returns 无医药列 → 收益按 0 计）
        assert daily_df["benchmark_return"].iloc[0] == pytest.approx(0.3 * 0.03 + 0.7 * 0.0)

    def test_pure_sector_bet(self, synth_benchmark_weights, synth_sw_returns):
        """全配银行（行业赌注），选股效应应接近 0。"""
        dates = synth_benchmark_weights.index
        records = []
        for d in dates:
            records.append({"date": d, "银行_weight": 1.0, "银行_return": 0.03,
                           "portfolio_return": 0.03,
                           "食品饮料_weight": 0.0, "食品饮料_return": 0.01})
        holdings_df = pd.DataFrame(records).set_index("date")

        daily_df = _compute_brinson_daily(holdings_df, synth_benchmark_weights, synth_sw_returns)
        assert not daily_df.empty
        # 全配银行(1.0) vs 基准(0.3)，行业配置效应应为正（超配涨了的行业）
        assert daily_df["allocation"].sum() > 0, "超配上涨行业应产生正配置效应"

    def test_pure_stock_selection(self, synth_benchmark_weights, synth_sw_returns):
        """行业权重与基准一致，选股效应体现。"""
        dates = synth_benchmark_weights.index
        records = []
        for d in dates:
            # 策略行业权重 = 基准权重
            records.append({
                "date": d,
                "银行_weight": 0.30, "银行_return": 0.05,   # 策略选股跑赢行业 4%
                "食品饮料_weight": 0.20, "食品饮料_return": 0.03,  # 策略选股=行业
                "portfolio_return": 0.30 * 0.05 + 0.20 * 0.03,
            })
        holdings_df = pd.DataFrame(records).set_index("date")

        daily_df = _compute_brinson_daily(holdings_df, synth_benchmark_weights, synth_sw_returns)
        assert not daily_df.empty
        # 行业权重匹配，配置效应应接近 0
        assert abs(daily_df["allocation"].sum()) < 0.01
        # 选股效应应显著为正（银行跑赢行业）
        assert daily_df["selection"].sum() > 0


class TestAggregatePeriod:
    def test_summary_output(self, synth_benchmark_weights, synth_sw_returns):
        """验证聚合输出结构完整。"""
        dates = synth_benchmark_weights.index
        daily_records = []
        holdings_records = []
        for d in dates:
            daily_records.append({
                "date": d,
                "portfolio_return": 0.012,
                "benchmark_return": 0.010,
                "excess_return": 0.002,
                "allocation": 0.001,
                "selection": 0.0005,
                "interaction": 0.0005,
                "unexplained": 0.0,
            })
            holdings_records.append({
                "date": d,
                "银行_weight": 0.50, "银行_return": 0.01,
                "食品饮料_weight": 0.20, "食品饮料_return": 0.03,
            })
        daily_df = pd.DataFrame(daily_records).set_index("date")
        holdings_df = pd.DataFrame(holdings_records).set_index("date")

        result = _aggregate_period(daily_df, holdings_df, synth_benchmark_weights, synth_sw_returns)

        assert "summary" in result
        assert "industry_detail" in result
        assert "daily" in result
        assert "exposure_summary" in result

        s = result["summary"]
        for k in ["total_portfolio_return", "allocation_effect", "selection_effect",
                   "interaction_effect", "unexplained"]:
            assert k in s

        # 银行超配 20%，应在 detail 中
        assert "银行" in result["industry_detail"]
        assert abs(result["industry_detail"]["银行"]["active_weight"] - 0.20) < 0.01

        # exposure_summary
        assert result["exposure_summary"]["max_single_industry_name"] == "银行"


# ═══════════════════════════════════════════
# 真库冒烟测试
# ═══════════════════════════════════════════


@pytest.mark.real_db
class TestRealDBAttribution:
    """在真实回测 DB 上跑完整归因流程。"""

    def test_full_attribution_pipeline(self, tmp_path):
        """使用 fixtures 中的 bars 合成一次简易回测 DB，跑归因。"""
        # 1. 制造一个简单的回测 DB
        db_path = str(tmp_path / "test_backtest.db")
        backtest_conn = sqlite3.connect(db_path)
        backtest_conn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trigger TEXT NOT NULL DEFAULT 'MANUAL',
                price REAL NOT NULL DEFAULT 0,
                shares INTEGER NOT NULL,
                turnover REAL NOT NULL DEFAULT 0,
                commission REAL NOT NULL DEFAULT 0,
                stamp_tax REAL NOT NULL DEFAULT 0,
                transfer_fee REAL NOT NULL DEFAULT 0,
                slippage_amount REAL NOT NULL DEFAULT 0,
                net_amount REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS account_daily (
                date TEXT PRIMARY KEY,
                cash REAL NOT NULL,
                total_value REAL NOT NULL,
                daily_pnl REAL NOT NULL DEFAULT 0,
                cumulative_pnl REAL NOT NULL DEFAULT 0,
                initial_capital REAL NOT NULL,
                n_holdings INTEGER NOT NULL DEFAULT 0
            );
        """)

        # 插入模拟交易: 大量买入银行股，少量食品饮料
        trades = [
            ("20240603", "600036.SH", "BUY", 50000),
            ("20240603", "601398.SH", "BUY", 100000),
            ("20240603", "600519.SH", "BUY", 100),
        ]
        for i, (d, s, side, sh) in enumerate(trades):
            backtest_conn.execute(
                "INSERT INTO trade_log (id, date, symbol, side, shares) VALUES (?,?,?,?,?)",
                (i+1, d, s, side, sh),
            )
        backtest_conn.commit()
        backtest_conn.close()

        # 2. 跑归因
        import os

        try:
            from adapters.tushare import _DEFAULT_DB_PATH
        except ImportError:
            pytest.skip("adapters/tushare.py 未配置（请从 .template 复制并填写数据库路径）")

        db_path_real = _DEFAULT_DB_PATH
        if not db_path_real or not os.path.exists(db_path_real):
            pytest.skip("真实 tushare 数据库不可用，跳过归因集成测试")
        result = brinson_attribute(db_path, db_path_real, "20240601", "20240701")

        # 3. 检查输出结构
        assert "summary" in result
        assert "industry_detail" in result
        assert len(result["daily"]) > 0

        # 银行应占大头
        ind_detail = result["industry_detail"]
        assert "银行" in ind_detail
        assert ind_detail["银行"]["avg_portfolio_weight"] > 0.5


# ═══════════════════════════════════════════
# brinson_attribute_from_files 测试
# ═══════════════════════════════════════════


class TestBrinsonFromFiles:
    """使用合成 parquet 文件测试 brinson_attribute_from_files。"""

    def test_full_pipeline_from_files(self, tmp_path):
        """合成 parquet 文件 → brinson_attribute_from_files → 验证输出结构。"""
        # 创建回测 DB（trade_log）
        db_path = str(tmp_path / "test_backtest.db")
        bconn = sqlite3.connect(db_path)
        bconn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                trigger TEXT NOT NULL DEFAULT 'MANUAL',
                price REAL NOT NULL DEFAULT 0,
                shares INTEGER NOT NULL,
                turnover REAL NOT NULL DEFAULT 0,
                commission REAL NOT NULL DEFAULT 0,
                stamp_tax REAL NOT NULL DEFAULT 0,
                transfer_fee REAL NOT NULL DEFAULT 0,
                slippage_amount REAL NOT NULL DEFAULT 0,
                net_amount REAL NOT NULL DEFAULT 0,
                reason TEXT NOT NULL DEFAULT '',
                run_id INTEGER NOT NULL DEFAULT 1
            );
        """)
        trades = [
            (1, "20240603", "600036.SH", "BUY", 10000),
            (2, "20240603", "600519.SH", "BUY", 2000),
            (3, "20240604", "600036.SH", "BUY", 5000),
            (4, "20240605", "600036.SH", "SELL", 5000),
            (5, "20240606", "600519.SH", "SELL", 1000),
        ]
        for tid, d, s, side, sh in trades:
            bconn.execute(
                "INSERT INTO trade_log (id, date, symbol, side, shares, run_id) "
                "VALUES (?,?,?,?,?, 1)",
                (tid, d, s, side, sh),
            )
        bconn.commit()
        bconn.close()

        # 创建合成 parquet 文件
        out_dir = str(tmp_path / "parquet_data")
        import os
        os.makedirs(out_dir, exist_ok=True)

        # industry_map
        ind_map = pd.DataFrame([
            {"ts_code": "600036.SH", "l1_name": "银行"},
            {"ts_code": "600519.SH", "l1_name": "食品饮料"},
        ])
        ind_map.to_parquet(f"{out_dir}/industry_map.parquet", index=False)

        # sw_returns
        dates = ["20240603", "20240604", "20240605", "20240606", "20240607"]
        sw_records = []
        for d in dates:
            sw_records.append({"trade_date": d, "name": "银行", "pct_change": 3.0})
            sw_records.append({"trade_date": d, "name": "食品饮料", "pct_change": 1.0})
        sw_df = pd.DataFrame(sw_records)
        sw_wide = sw_df.pivot_table(
            index="trade_date", columns="name", values="pct_change", aggfunc="first"
        )
        sw_wide.to_parquet(f"{out_dir}/sw_returns.parquet")

        # benchmark_weights
        bw_records = []
        for d in dates:
            bw_records.append({"trade_date": d, "l1_name": "银行", "weight": 0.30})
            bw_records.append({"trade_date": d, "l1_name": "食品饮料", "weight": 0.20})
        bw_df = pd.DataFrame(bw_records)
        bw_wide = bw_df.pivot_table(
            index="trade_date", columns="l1_name", values="weight",
            aggfunc="first", fill_value=0.0,
        )
        bw_wide.to_parquet(f"{out_dir}/benchmark_weights.parquet")

        # bars (MultiIndex)
        bar_records = []
        for i, d in enumerate(dates):
            bar_records.append({
                "trade_date": d, "symbol": "600036.SH",
                "close": 35.0 * (1 + 0.02 * i), "pct_chg": 2.0,
            })
            bar_records.append({
                "trade_date": d, "symbol": "600519.SH",
                "close": 1600.0 * (1 + 0.02 * i), "pct_chg": 5.0,
            })
        bars_df = pd.DataFrame(bar_records)
        bars_df.set_index(["trade_date", "symbol"], inplace=True)
        bars_df.to_parquet(f"{out_dir}/bars.parquet")

        # 调用 brinson_attribute_from_files
        result = brinson_attribute_from_files(
            result_db=db_path,
            industry_map=f"{out_dir}/industry_map.parquet",
            sw_returns=f"{out_dir}/sw_returns.parquet",
            benchmark_weights=f"{out_dir}/benchmark_weights.parquet",
            bars=f"{out_dir}/bars.parquet",
            run_id=1,
        )

        # 验证输出结构
        assert "summary" in result
        assert "industry_detail" in result
        assert "daily" in result
        assert "exposure_summary" in result
        assert "error" not in result, f"Unexpected error: {result.get('error')}"

        s = result["summary"]
        for k in ["total_portfolio_return", "allocation_effect", "selection_effect",
                   "interaction_effect", "unexplained"]:
            assert k in s, f"Missing key {k} in summary"

        # 银行应出现在 industry_detail 中
        assert "银行" in result["industry_detail"]

        # 验证 attribution 恒等式: excess = allocation + selection + interaction + unexplained
        excess = s["total_excess_return"]
        components = (s["allocation_effect"] + s["selection_effect"]
                      + s["interaction_effect"] + s["unexplained"])
        assert abs(excess - components) < 1e-8, (
            f"Attribution identity violated: excess={excess}, components={components}"
        )

    def test_missing_file_raises(self, tmp_path):
        """缺少 parquet 文件应抛出 FileNotFoundError。"""
        db_path = str(tmp_path / "test.db")
        bconn = sqlite3.connect(db_path)
        bconn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, side TEXT,
                shares INTEGER, run_id INTEGER DEFAULT 1
            );
        """)
        bconn.close()

        nonexistent = str(tmp_path / "nonexistent.parquet")
        with pytest.raises(FileNotFoundError, match="industry_map"):
            brinson_attribute_from_files(
                result_db=db_path,
                industry_map=nonexistent,
                sw_returns=nonexistent,
                benchmark_weights=nonexistent,
                bars=nonexistent,
            )

    def test_empty_sw_returns(self, tmp_path):
        """空 sw_returns 返回 error。"""
        db_path = str(tmp_path / "test.db")
        bconn = sqlite3.connect(db_path)
        bconn.executescript("""
            CREATE TABLE IF NOT EXISTS trade_log (
                id INTEGER PRIMARY KEY, date TEXT, symbol TEXT, side TEXT,
                shares INTEGER, run_id INTEGER DEFAULT 1
            );
        """)
        bconn.close()

        out_dir = str(tmp_path / "parquet_data")
        import os
        os.makedirs(out_dir, exist_ok=True)

        pd.DataFrame({"ts_code": [], "l1_name": []}).to_parquet(
            f"{out_dir}/industry_map.parquet", index=False
        )
        pd.DataFrame().to_parquet(f"{out_dir}/sw_returns.parquet")
        pd.DataFrame().to_parquet(f"{out_dir}/benchmark_weights.parquet")
        pd.DataFrame().to_parquet(f"{out_dir}/bars.parquet")

        result = brinson_attribute_from_files(
            result_db=db_path,
            industry_map=f"{out_dir}/industry_map.parquet",
            sw_returns=f"{out_dir}/sw_returns.parquet",
            benchmark_weights=f"{out_dir}/benchmark_weights.parquet",
            bars=f"{out_dir}/bars.parquet",
        )
        assert "error" in result
        assert result["error"] == "sw_returns 为空"
