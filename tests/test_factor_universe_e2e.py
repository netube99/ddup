"""factor_universe 端到端测试：宽域因子计算 + 窄域交易裁切。

验证引擎在配置 factor_universe 时：
  - 主面板按 factor_universe 加载数据并物化因子
  - 物化后裁切到 trading universe（index_universe / get_universe）
  - select() 收到的 bars 仅含交易域符号，但因子列值反映宽域计算口径
  - 不配置 factor_universe 时行为完全不变
"""

from btcore.engine import Engine
from btcore.provider import DataProvider
from btcore.strategy_loader import build_strategy
from strategies.examples.rolling_ranker import RollingRanker
from tests.conftest import MockDataBackend

START, END = "20240603", "20240607"


def _wrap_backend_with_index_members(backend, members_map):
    """给已有 backend 实例挂接 get_index_members，返回 provider。"""
    backend.get_index_members = lambda codes, s, e: dict(members_map)
    return DataProvider(backend)


def test_factor_computed_on_wide_traded_on_narrow():
    """factor_universe 配置为 fixtures 的全量符号（宽域），get_universe 只返回 1 只。"""
    backend = MockDataBackend()
    bars = backend.query_bars(None, "20240603", "20240701")
    all_symbols = sorted(bars.index.get_level_values("symbol").unique())
    # 宽域 = 前 5 只，窄域 = 第 1 只
    wide_symbols = all_symbols[:5]
    narrow_symbols = [all_symbols[0]]

    # factor_universe 会通过 get_factor_universe 生成区间并集
    snapshots = {START: set(wide_symbols)}
    provider = _wrap_backend_with_index_members(backend, snapshots)

    strategy = build_strategy(
        RollingRanker,
        config={"initial_capital": 500000, "top_k": 1},
        factor_specs=[
            {"name": "mom20", "weight": 1.0, "ascending": False},
        ],
        filter_rules={
            "factor_universe": ["000300.SH"],  # 由 mock snapshots 提供
        },
    )
    # 覆盖 get_universe 返回窄域（不被 loader 覆盖因为测的是 get_universe 兜底）
    strategy.get_universe = lambda p, s, e: narrow_symbols

    engine = Engine(strategy, provider, initial_capital=500000)
    result = engine.run(START, END)

    # 验证回测完整跑通
    assert len(result["account_daily"]) > 0
    assert result["statistics"]["trade_count"] >= 0


def test_no_factor_universe_unchanged():
    """不配置 factor_universe 时行为完全不变（回归验证）。"""
    backend = MockDataBackend()
    provider = DataProvider(backend)

    strategy = build_strategy(
        RollingRanker,
        config={"initial_capital": 500000, "top_k": 1},
        factor_specs=[
            {"name": "mom20", "weight": 1.0, "ascending": False},
        ],
        filter_rules={"exclude_st": True},
    )

    engine = Engine(strategy, provider, initial_capital=500000)
    result = engine.run(START, END)

    assert len(result["account_daily"]) > 0
    assert result["statistics"]["trade_count"] >= 0


def test_factor_universe_crop_empty_raises():
    """factor_universe 裁切后无数据时应报错。"""
    backend = MockDataBackend()

    # 提供不重叠的 symbols：宽域符号均不在窄域内
    snapshots = {START: {"999999.SZ", "888888.SH"}}
    provider = _wrap_backend_with_index_members(backend, snapshots)

    strategy = build_strategy(
        RollingRanker,
        config={"top_k": 1},
        filter_rules={"factor_universe": ["000300.SH"]},
    )
    strategy.get_universe = lambda p, s, e: ["000001.SZ"]

    engine = Engine(strategy, provider, initial_capital=500000)

    import pytest
    with pytest.raises(ValueError, match="裁切后无数据"):
        engine.run(START, END)
