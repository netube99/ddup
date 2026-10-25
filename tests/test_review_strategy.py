"""策略层 backlog 清理的独立回归（F-RSR-04/05/08/09/11/12）。

全部用手工构造的 bars/成交回报与手算期望，不依赖行情数据。
"""

import importlib
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest
import yaml

from tests.conftest import make_account, make_bar

STRATEGIES_DIR = Path(__file__).resolve().parents[1] / "strategies"
ALL_CONFIGS = sorted(
    list(STRATEGIES_DIR.glob("examples/*/config.yaml"))
    + list(STRATEGIES_DIR.glob("selected/*/config.yaml"))
    + list(STRATEGIES_DIR.glob("exploring/*/config.yaml"))
)


# ── F-RSR-04：config 顶层键重复（PyYAML last-wins 掩盖失效路径）────────────

def test_no_duplicate_top_level_keys():
    for path in ALL_CONFIGS:
        with open(path, encoding="utf-8") as f:
            root = yaml.compose(f)
        keys = [key.value for key, _ in root.value]
        assert len(keys) == len(set(keys)), (
            f"{path}: 顶层键重复 {sorted({k for k in keys if keys.count(k) > 1})}"
        )


# ── F-RSR-05：calc_conditions 循环内 conds.remove 跳过下一元素 ──────────────

@pytest.mark.parametrize("module_name, cls_name", [
    ("strategies.examples.rolling_ranker.strategy", "RollingRanker"),
    ("strategies.examples.multi_model.strategy", "MultiModel"),
])
def test_take_profit_removal_keeps_stop_loss_adaptation(module_name, cls_name):
    """TAKE_PROFIT 在首位被移除后，紧随其后的 STOP_LOSS 仍须完成新仓紧止损。"""
    cls = getattr(importlib.import_module(module_name), cls_name)
    s = cls(config={})
    s._cond = SimpleNamespace(calc=lambda *a: [
        {"type": "TAKE_PROFIT", "price": 12.0},
        {"type": "STOP_LOSS", "price": 9.4},
    ])

    conds = s.calc_conditions("S1", 10.0, make_bar(pct_chg=0.01, turnover_rate=0.01), 1)

    assert not any(c["type"] == "TAKE_PROFIT" for c in conds)
    sl = next(c for c in conds if c["type"] == "STOP_LOSS")
    assert sl["price"] == pytest.approx(10.0 * 0.97)


# ── F-RSR-08：调仓间隔/冷却期须按交易日计数（E2E-01 口径）───────────────────

REBALANCE_CASES = [
    ("strategies.exploring.defensive_vol_gate.strategy", "DefensiveVolGate",
     "_interval", {"_use_gate": False, "_band_ratio": 1.0}),
    ("strategies.exploring.loss_contrarian_300.strategy", "LossContrarian300",
     "_rebalance_interval", {}),
    ("strategies.exploring.ml_lowcrowd.strategy", "MLowcrowd",
     "_rebalance_interval", {}),
    ("strategies.exploring.pension_track.strategy", "PensionTrack",
     "_rebalance_interval", {"_pool_mode": "tilt", "_band_ratio": 1.0}),
]


@pytest.mark.parametrize("module_name, cls_name, interval_attr, extra", REBALANCE_CASES)
def test_rebalance_interval_counts_trading_days(
    module_name, cls_name, interval_attr, extra, monkeypatch
):
    """interval=20 交易日 → 恰在第 0、20 个决策日调仓；跨月相邻交易日不得强插。

    旧实现 int(YYYYMMDD) 差：20240201-20240131=70 ≥ 20 → 月末调仓次日立即再调。
    """
    mod = importlib.import_module(module_name)
    cls = getattr(mod, cls_name)
    rebalance_dates: list[str] = []

    def fake_eval(df, specs):
        rebalance_dates.append(df["trade_date"].iloc[0])
        return None, pd.Series(1.0, index=df.index)

    monkeypatch.setattr(mod, "eval_factor_specs", fake_eval)

    s = cls(config={})
    s._top_k = 5
    setattr(s, interval_attr, 20)
    for k, v in extra.items():
        setattr(s, k, v)
    # 兼容新旧两套状态变量：未被读取的一侧是惰性属性
    s._last_rebalance = 0
    s._days_since_rebalance = 999

    dates = [d.strftime("%Y%m%d") for d in pd.bdate_range("2024-01-31", periods=22)]
    account = make_account()
    for d in dates:
        bars = {"S1": make_bar(date=d), "S2": make_bar(date=d, open=20.0)}
        s.select(bars, account, None)

    assert rebalance_dates == [dates[0], dates[20]]


COOLDOWN_CASES = [
    ("strategies.exploring.re_attack_500.strategy", 3),
    ("strategies.exploring.dv_tg_barbell.strategy", 30),
]


@pytest.mark.parametrize("module_name, cooldown_days", COOLDOWN_CASES)
def test_cooldown_counts_trading_days(module_name, cooldown_days):
    """月末成交的冷却期按交易日递减；跨月次日不得立即过期。"""
    mod = importlib.import_module(module_name)
    if module_name.endswith("re_attack_500.strategy"):
        s = mod.ReAttack500(config={})
        trigger, cooldown_attr = "STOP_LOSS", "_cooldown_days"
    else:
        s = mod.DvTgBarbell(config={})
        trigger, cooldown_attr = "TREND_BREAK", "_tg_cooldown"
    s._cooldown = {}
    s._book = {}
    s._pending_book = {}
    setattr(s, cooldown_attr, cooldown_days)
    s._days_since_rebalance = 0

    s.on_fills([SimpleNamespace(
        side="SELL", trigger=trigger, date="20240131", symbol="S1", price=10.0
    )], None)
    assert "S1" in s._cooldown

    days = [d.strftime("%Y%m%d") for d in pd.bdate_range("2024-02-01", periods=cooldown_days + 4)]
    for i, d in enumerate(days, start=1):
        s.on_tick({"S1": make_bar(date=d)}, None, None)
        if i <= cooldown_days:
            assert "S1" in s._cooldown, f"第 {i} 个交易日冷却期内不应过期"
        else:
            assert "S1" not in s._cooldown, f"第 {i} 个交易日冷却期应已解除"


# ── F-RSR-09：bear_gating 依赖的 pct_above_ma20 须进 factor_specs 闭包 ─────

def test_trend_guard_closure_includes_breadth_column():
    from btcore.strategy_loader import load_strategy

    strategy = load_strategy(
        str(STRATEGIES_DIR / "selected" / "trend_guard_bw_300" / "config.yaml")
    )
    assert "pct_above_ma20" in strategy.FACTOR_NODES


def test_trend_guard_bear_regime_activates_with_breadth():
    from strategies.selected.trend_guard_bw_300.strategy import TrendGuard

    s = TrendGuard(config={})
    s._gating_enabled = True
    s._bear_regime = False
    s._bear_counter = 0
    s._bull_counter = 0
    s._bear_threshold = TrendGuard.BEAR_BREADTH_THRESHOLD
    s._bull_threshold = TrendGuard.BULL_RECOVERY_THRESHOLD

    for _ in range(TrendGuard.BEAR_CONFIRM_DAYS):
        s._update_regime({"S1": {"pct_above_ma20": 0.10}})

    assert s._bear_regime is True


# ── F-RSR-11：condition_hunter 须声明 turnover_rate 扩展列 ─────────────────

def test_condition_hunter_declares_turnover_rate():
    from strategies.examples.condition_hunter.strategy import ConditionHunter

    assert "turnover_rate" in ConditionHunter.REQUIRED_FIELDS


# ── F-RSR-12：pct_sealed 门控须保留满额 window 个历史值 ────────────────────

def test_gate_history_retains_window_history_plus_today():
    """声明口径：60 日中位不含今日 → 截断上限须为 window+1。"""
    from strategies.selected.core_lowvol_500.strategy import (
        CoreLowVol500,
        gate_triggered,
    )

    s = CoreLowVol500(config={"timing_gate": True})
    s._gate_on = True
    s._gate_window = 60
    s._sealed_hist = []
    s._cooldown = {}

    # 60 个历史值：30 个 0.01 + 30 个 0.03 → 60 值中位 = (0.01+0.03)/2 = 0.02
    for v in [0.01] * 30 + [0.03] * 30 + [0.025]:
        s.on_tick({"S1": make_bar(pct_sealed=v)}, None, None)

    assert len(s._sealed_hist) == 61
    # 今日 0.025 ≥ 60 值中位 0.02 → 不触发清仓（59 值口径中位 0.03 → 会误触发）
    assert gate_triggered(s._sealed_hist, 60) is False
