"""TDD 正确性审查 — ML 子系统（labels/build_guard_samples 训练-推理同源）。

背景：引擎侧 ret_from_entry 的定义源是 holding.entry_price 的逐日路径
（加仓日 manual.py 重算 cost/shares、除权除息日 corporate._rescale_holding
缩放），而标签重放（extract_trade_pairs → build_guard_samples）此前用
静态回合聚合 buy_price 充当全部持仓日的 entry_price——金字塔加仓与
期内除息场景下训练特征与推理特征静默漂移（违反 docs/ml_guide.md §1
"账户态特征的计算公式训练侧重放与引擎推理共用同一定义"）。
"""


import pandas as pd
import pytest

from btcore import database
from btcore.ml.labels import build_guard_samples, extract_trade_pairs
from tests.conftest import make_spec
from tests.test_ml import _db


class TestRetFromEntryPath:
    def test_pyramid_round_entry_path(self, tmp_path):
        """加仓回合：加仓前的持仓日 ret_from_entry 必须用加仓前 entry_price。

        引擎口径（match/manual.py 加仓重算 cost/shares）：d1 entry=10，
        d2 起 entry=(1000+1200)/200=11。重放若恒用聚合 buy_price=11，
        d1 的 ret_from_entry = close/11 而非 close/10（静默漂移）。
        """
        db = _db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100, -1000.0),
            ("20240103", "X", "BUY", "TARGET", 12.0, 100, -1200.0),
            ("20240110", "X", "SELL", "MANUAL", 9.0, 200, 1800.0),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1

        dates = ["20240102", "20240103", "20240104", "20240105",
                 "20240108", "20240109", "20240110"]
        idx = pd.MultiIndex.from_tuples(
            [(d, "X") for d in dates], names=["trade_date", "symbol"]
        )
        panel = pd.DataFrame(
            {"mom20": 1.0,
             "close": [11.0, 11.5, 12.0, 12.5, 12.0, 11.0, 10.0],
             "pre_close": 10.0},
            index=idx,
        )
        samples = build_guard_samples(panel, pairs, make_spec(), lookahead=0)

        by_date = dict(zip(samples["trade_date"], samples["ret_from_entry"]))
        # 引擎口径逐日重放：d1 entry=10 → 11/10-1；d2 起 entry=11
        assert by_date["20240102"] == pytest.approx(11.0 / 10 - 1)
        for d, close in zip(dates[1:6], [11.5, 12.0, 12.5, 12.0, 11.0]):
            assert by_date[d] == pytest.approx(close / 11 - 1), d

    def test_cash_div_round_entry_rescale(self, tmp_path):
        """期内现金分红：除息日起 entry_price 必须按引擎口径 rescale。

        引擎（corporate._apply_cash_div）：cost -= net（税后），
        entry_price ×= pre_close/(pre_close + 每股税前红利)。
        税率按 (除息日 - entry_date) 日历天数分档：≤30 天 20%。
        构造：100 股 @10 买入，每股 0.5 红利 → gross=50, net=40，
        pre_close=10 → scale=10/10.5 → 除息日起 entry=10×10/10.5。
        """
        db = _db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100, -1000.0),
            ("20240110", "X", "DIV", "CORPORATE", 0.0, 0, 40.0),
            ("20240115", "X", "SELL", "MANUAL", 9.0, 100, 900.0),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1

        dates = ["20240102", "20240103", "20240104", "20240105",
                 "20240108", "20240109", "20240110", "20240111",
                 "20240112", "20240115"]
        idx = pd.MultiIndex.from_tuples(
            [(d, "X") for d in dates], names=["trade_date", "symbol"]
        )
        panel = pd.DataFrame(
            {"mom20": 1.0,
             "close": [10.2, 10.1, 9.9, 10.0, 10.3, 10.1, 9.8, 9.5, 9.6, 9.0],
             "pre_close": 10.0},
            index=idx,
        )
        samples = build_guard_samples(panel, pairs, make_spec(), lookahead=0)

        by_date = dict(zip(samples["trade_date"], samples["ret_from_entry"]))
        ep_pre = 10.0
        # 除息日当天 corporate.adjust 先于决策时点执行 → 当日 entry 已缩放
        ep_post = 10.0 * 10.0 / 10.5
        for d, close in zip(dates[:6], [10.2, 10.1, 9.9, 10.0, 10.3, 10.1]):
            assert by_date[d] == pytest.approx(close / ep_pre - 1), d
        for d, close in zip(dates[6:9], [9.8, 9.5, 9.6]):
            assert by_date[d] == pytest.approx(close / ep_post - 1), d


class TestEngineReplayParity:
    def test_ret_from_entry_matches_engine_entry_path(self, tmp_path):
        """端到端同源验收：期内除权除息回合的重放 ret_from_entry 与引擎
        decision 时点注入值逐日一致（entry_price 路径来自 debug 快照）。
        修复前重放恒用静态 buy_price（漏除息 rescale），除息日之后逐日
        漂移约每股红利/买入价（scratch 实证 -5.99% vs 引擎 -3.33%）。"""
        import json

        from btcore.engine import Engine
        from btcore.factors.library import load_library
        from btcore.ml import dataset
        from btcore.provider import DataProvider
        from btcore.strategy import Strategy
        from tests.conftest import MockDataBackend

        sym = "920826.BJ"  # 送转 0.20 + 派现 0.15，除息日 20240613

        class BuyHoldDiv(Strategy):
            def on_start(self, provider, first_date, end_date=None):
                self._done = False

            def select(self, bars, snapshot, provider):
                if self._done:
                    return {"buy": [], "sell": []}
                if not snapshot.holdings:
                    return {"buy": [sym], "sell": []}
                if snapshot.holdings[sym].holding_days >= 9:
                    self._done = True
                    return {"buy": [], "sell": [sym]}
                return {"buy": [], "sell": []}

            def calc_conditions(self, symbol, entry_price, bar, holding_days):
                return []

        db = str(tmp_path / "r.db")
        engine = Engine(
            BuyHoldDiv({"initial_capital": 1_000_000}),
            DataProvider(MockDataBackend()), db_path=db, debug=True,
        )
        engine.run("20240603", "20240624")

        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1

        spec = make_spec()
        panel = dataset.build_panel(
            MockDataBackend(), [sym], "20240603", "20240624",
            spec, load_library(), benchmark="000300.SH",
        )
        samples = build_guard_samples(panel, pairs, spec, lookahead=0)
        assert len(samples) >= 8

        conn = database.connect_result_db(db, read_only=True)
        engine_entry = {}
        for d, js in conn.execute(
            "SELECT date, snapshot_json FROM debug_snapshots"
        ).fetchall():
            h = json.loads(js)["holdings_detail"].get(sym)
            if h:
                engine_entry[d] = h["entry_price"]
        conn.close()

        bars = panel.loc[(slice(None), sym), :].droplevel("symbol")
        for row in samples.itertuples():
            close = bars.loc[row.trade_date, "close"]
            engine_ret = close / engine_entry[row.trade_date] - 1
            assert row.ret_from_entry == pytest.approx(engine_ret, abs=1e-9), (
                row.trade_date
            )


class TestReplayEdgeParity:
    def test_suspended_ex_date_no_rescale(self, tmp_path):
        """停牌除息日：引擎 scale=None 分支（corporate.py EDGE-12）——cost 照减
        但 entry_price 不缩放。重放若用恢复日 pre_close 补缩放（事件被
        events[k][0] <= d 排空延迟到下一交易日），除息日后逐日漂移。"""
        db = _db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100, -1000.0),
            ("20240104", "X", "DIV", "CORPORATE", 0.0, 0, 40.0),
            ("20240112", "X", "SELL", "MANUAL", 10.0, 100, 999.4),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 1

        dates = ["20240102", "20240103", "20240108", "20240109", "20240112"]
        idx = pd.MultiIndex.from_tuples(
            [(d, "X") for d in dates], names=["trade_date", "symbol"]
        )
        # 0104-0105 停牌缺行；0108 pre_close=9.5——错误路径会拿它补缩放
        panel = pd.DataFrame(
            {"mom20": 1.0,
             "close": [10.1, 10.0, 9.5, 9.6, 9.4],
             "pre_close": [10.0, 10.0, 9.5, 9.5, 9.5]},
            index=idx,
        )
        samples = build_guard_samples(panel, pairs, make_spec(), lookahead=0)
        by_date = dict(zip(samples["trade_date"], samples["ret_from_entry"]))
        # 卖出日 dts=0 被 lookahead 规则跳过（预期），其余 4 日必须产出样本：
        # 含恢复日 0108/0109——错误路径在这里用恢复日 pre_close 补缩放
        assert set(by_date) == set(dates) - {"20240112"}
        for d, ret in by_date.items():
            assert ret == pytest.approx(
                panel.loc[(d, "X"), "close"] / 10.0 - 1
            ), d

    def test_same_day_exit_then_reentry_two_rounds(self, tmp_path):
        """同日卖出→再买入：引擎盘中先卖后买，必须重构为两个回合。

        条件单路径可达（持仓 X 的条件卖单 + select 再发条件买），违反时
        re-buy 被并入未平仓回合当加仓：buy_date/entry 路径/pnl 全错。"""
        db = _db(tmp_path, [
            ("20240102", "X", "BUY", "MANUAL", 10.0, 100, -1000.0),
            ("20240105", "X", "SELL", "STOP_LOSS", 9.5, 100, 949.9),
            ("20240105", "X", "BUY", "LIMIT_BUY", 9.5, 100, -950.0),
            ("20240112", "X", "SELL", "TAKE_PROFIT", 10.5, 100, 1049.9),
        ])
        pairs = extract_trade_pairs(db)
        assert len(pairs) == 2

        r = pairs[pairs["symbol"] == "X"].sort_values("buy_date")
        assert list(r["buy_date"]) == ["20240102", "20240105"]
        assert r.iloc[0]["buy_price"] == pytest.approx(10.0)
        assert r.iloc[1]["buy_price"] == pytest.approx(9.5)
        assert r.iloc[0]["pnl"] == pytest.approx(-50.1)
        assert r.iloc[1]["pnl"] == pytest.approx(99.9)
        assert r.iloc[0]["trigger"] == "STOP_LOSS"
        assert r.iloc[1]["trigger"] == "TAKE_PROFIT"
