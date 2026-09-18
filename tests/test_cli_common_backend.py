"""DDUP_BACKEND 环境变量后端选择测试（research/cli_common.py）。"""

import sys
import types

import pytest

from research import cli_common


class _FakeBackend:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_default_backend_spec():
    assert cli_common.DEFAULT_BACKEND == "adapters.tushare:TushareBackend"


def test_load_backend_env_override(monkeypatch):
    mod = types.ModuleType("fake_gold_backend_for_test")
    mod.FakeBackend = _FakeBackend
    monkeypatch.setitem(sys.modules, "fake_gold_backend_for_test", mod)
    monkeypatch.setenv("DDUP_BACKEND", "fake_gold_backend_for_test:FakeBackend")
    backend = cli_common._load_backend()
    assert isinstance(backend, _FakeBackend)


def test_load_backend_bad_spec(monkeypatch):
    monkeypatch.setenv("DDUP_BACKEND", "no_colon_here")
    with pytest.raises(ValueError, match="module:Class"):
        cli_common._load_backend()


def test_load_backend_missing_class(monkeypatch):
    mod = types.ModuleType("fake_gold_backend_for_test2")
    monkeypatch.setitem(sys.modules, "fake_gold_backend_for_test2", mod)
    monkeypatch.setenv("DDUP_BACKEND", "fake_gold_backend_for_test2:NoSuchClass")
    with pytest.raises(ValueError, match="不存在"):
        cli_common._load_backend()
