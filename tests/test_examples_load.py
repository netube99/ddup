"""示例策略烟雾测试：strategies/examples/*/config.yaml 全量 load_strategy 通过。

示例 config 与 loader 校验漂移无兜底（整改指导 TEST-03）——每加一个示例
必须有 load 级回归。只 load 不 run（run 需要完整行情与撮合，超出本测试范围）。
"""

from pathlib import Path

import pytest

from btcore.strategy_loader import load_strategy

EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "strategies" / "examples"


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
