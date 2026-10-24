"""value_momentum 基准趋势感知依赖 idx_ret 伪列（F-RSR-07 回归）。

伪列契约（docs/strategy_guide.md bar 表）：idx_ret 仅因子表达式引用时附着。
策略 on_tick 读取 bar["idx_ret"] 做 30 日基准趋势切换，因此 factor_specs 必须以
materialize_only 声明一个引用 idx_ret 的因子（bench_ret），否则面板无该列、
_bull_mode 在窗口填满后永久 False，趋势感知静默失效且无告警。
"""

from pathlib import Path

from btcore.factors import plan as factor_plan
from btcore.strategy_loader import load_strategy

CONFIG = Path("strategies/selected/value_momentum/config.yaml")


def _loaded():
    strategy = load_strategy(str(CONFIG))
    names = [s["name"] for s in strategy.FACTOR_SPECS]
    return strategy, names


def test_factor_plan_requests_index_pseudo_column():
    """闭包引用 idx_ret（经 bench_ret）→ 因子计划需要指数基准数据。"""
    strategy, names = _loaded()
    fplan = factor_plan.build_factor_plan(strategy.FACTOR_NODES, names)
    assert fplan["needs"]["index"] is True


def test_bench_ret_is_materialize_only_and_not_scored():
    """bench_ret 仅物化不评分：评分权重仍由五大价值/动量因子构成。"""
    strategy, _ = _loaded()
    bench = [s for s in strategy.FACTOR_SPECS if s["name"] == "bench_ret"]
    assert len(bench) == 1
    assert bench[0].get("materialize_only") is True
    scored = [s for s in strategy.FACTOR_SPECS if not s.get("materialize_only")]
    assert "bench_ret" not in [s["name"] for s in scored]
    assert sum(float(s.get("weight", 1.0)) for s in scored) == 1.0


def test_engine_run_attaches_idx_ret_to_bars():
    """端到端：闭包引用 idx_ret（经 bench_ret）时引擎附着该伪列到决策 bars。"""
    from btcore.engine import Engine
    from btcore.provider import DataProvider
    from btcore.strategy_loader import build_strategy
    from strategies.examples.rolling_ranker import RollingRanker
    from tests.conftest import MockDataBackend

    strategy = build_strategy(
        RollingRanker,
        config={"initial_capital": 500000, "top_k": 1},
        factor_specs=[
            {"name": "mom20", "weight": 1.0, "ascending": False},
            {"name": "bench_ret", "materialize_only": True},
        ],
    )
    seen = []
    orig_on_tick = strategy.on_tick

    def recorder(bars, snapshot, provider):
        if bars:
            seen.append(next(iter(bars.values())).get("idx_ret"))
        return orig_on_tick(bars, snapshot, provider)

    strategy.on_tick = recorder
    engine = Engine(strategy, DataProvider(MockDataBackend()), initial_capital=500000)
    result = engine.run("20240603", "20240628")
    assert len(result["account_daily"]) > 0
    assert seen and all(v is not None for v in seen)
    # 首日 idx_ret 可为 NaN（基准 pct_change 无前日），其余应为真实基准日收益
    valid = [v for v in seen if v == v]
    assert len(valid) >= len(seen) - 1
    assert any(v != 0.0 for v in valid), "idx_ret 全 0：基准数据未真正到达决策点"


class _StubBackend:
    pass


class _StubProvider:
    backend = _StubBackend()


class _Snap:
    holdings: dict = {}


def test_on_tick_treats_nan_idx_ret_as_zero():
    """首日基准 pct_change 为 NaN 时按 0 计，不得污染趋势窗口求和。"""
    strategy = load_strategy(str(CONFIG))
    strategy.on_start(_StubProvider(), "20240101", "20240630")
    strategy.on_tick(
        {"S1": {"trade_date": "20240102", "close": 10.0, "idx_ret": float("nan")}},
        _Snap(),
        None,
    )
    assert strategy._bench_returns == [0.0]
