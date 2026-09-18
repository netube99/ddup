"""示例策略烟雾测试：strategies/examples/*/config.yaml 全量 load_strategy 通过。

示例 config 与 loader 校验漂移无兜底（整改指导 TEST-03）——每加一个示例
必须有 load 级回归。只 load 不 run（run 需要完整行情与撮合，超出本测试范围）。
"""

from pathlib import Path

import pytest

from btcore.strategy_loader import load_strategy

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "strategies" / "examples"
SELECTED_DIR = Path(__file__).resolve().parents[1] / "strategies" / "selected"
EXPLORING_DIR = Path(__file__).resolve().parents[1] / "strategies" / "exploring"


def _example_configs() -> list[Path]:
    return sorted(EXAMPLES_DIR.glob("*/config.yaml"))


def test_examples_present():
    """示例目录存在且非空（glob 返回空时其余用例会静默通过，这里兜底）。"""
    configs = _example_configs()
    assert len(configs) >= 7, f"示例 config 少于 7 个: {[p.parent.name for p in configs]}"


@pytest.mark.parametrize("path", [str(p) for p in _example_configs()])
def test_example_config_loads(path):
    """每个示例 config.yaml 都能完整通过 load_strategy（不 run）。"""
    strategy = load_strategy(path)
    assert strategy is not None
    # FACTOR_NODES 闭包已解析（因子名/依赖在加载期全部校验）
    assert isinstance(getattr(strategy, "FACTOR_NODES", None), dict)


def _selected_exploring_configs() -> list[Path]:
    return sorted(
        list(SELECTED_DIR.glob("*/config.yaml"))
        + list(EXPLORING_DIR.glob("*/config.yaml"))
    )


def test_selected_exploring_present():
    """selected/exploring 目录存在且非空（兜底，防 glob 空转静默通过）。"""
    configs = _selected_exploring_configs()
    assert len(configs) >= 3, f"策略 config 少于 3 个: {[p.parent.name for p in configs]}"


@pytest.mark.parametrize("path", [str(p) for p in _selected_exploring_configs()])
def test_selected_exploring_config_loads(path):
    """selected/exploring 策略 config 同样全量 load_strategy（含 ML models 节）。

    无因子策略（账本回放驱动等）FACTOR_SPECS 为空，FACTOR_NODES 保持 None
    （Engine._build_factor_plan 对空 specs 直接跳过）；有 spec 必须是 dict。
    """
    strategy = load_strategy(path)
    assert strategy is not None
    assert not strategy.FACTOR_SPECS or isinstance(
        getattr(strategy, "FACTOR_NODES", None), dict
    )


def test_gate_triggered_logic():
    """pct_sealed 择时门控纯函数：低于历史中位触发、冷启动不触发、边界正确。"""
    from strategies.selected.core_lowvol_500.strategy import gate_triggered

    # 冷启动：历史不足 20 日不触发
    short = [(20240101 + i, 0.02) for i in range(10)]
    assert not gate_triggered(short, 60)

    # 构造 60 日历史：50 天高值 + 9 天中值，今日低值 → 触发
    hist = [(20240101 + i, 0.03 if i % 2 == 0 else 0.025) for i in range(59)]
    low_today = hist + [(20240101 + 60, 0.005)]
    assert gate_triggered(low_today, 60)
    # 今日高值 → 不触发
    high_today = hist + [(20240101 + 60, 0.05)]
    assert not gate_triggered(high_today, 60)
    # 窗口截取：只比较最近 window 个历史值
    stale_high = [(20240101, 0.001)] + [(20240101 + i, 0.03) for i in range(1, 59)]
    assert gate_triggered(stale_high + [(20240101 + 60, 0.005)], 60)
