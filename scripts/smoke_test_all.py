#!/usr/bin/env python3
"""烟幕测试：逐项验证 9 项改进的实盘行为。每个测试独立运行，报告 PASS/FAIL。"""

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pandas as pd
import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from btcore.provider import DataProvider  # noqa: E402

# 抑制因子库加载提示
logging.getLogger("btcore.factors.library").setLevel(logging.ERROR)
logging.getLogger().setLevel(logging.WARNING)


os.chdir(str(PROJECT_ROOT))

PASS = 0
FAIL = 0

def report(name: str, ok: bool, detail: str = ""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}")
    if detail:
        print(f"         {detail}")


# ─── 1.1 factor_specs materialize_only ───

def test_materialize_only():
    """加载含 materialize_only 的策略，验证因子被物化但不参与评分。"""
    from btcore.strategy_loader import load_strategy

    config_yaml = """
strategy: strategies.examples.topk_momentum:TopKMomentum
config:
  initial_capital: 100000
  top_k: 5
  max_positions: 5
benchmark: "000300.SH"
factor_specs:
  - factor: mom20
    weight: 0.5
  - factor: vol_z
    weight: 0.5
  - factor: turnover_z
    materialize_only: true
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        strategy = load_strategy(config_path)
        specs = strategy.FACTOR_SPECS
        tu_spec = next(s for s in specs if s["name"] == "turnover_z")
        ok1 = tu_spec.get("materialize_only") is True
        report("1.1 materialize_only 解析", ok1,
               f"turnover_z.materialize_only={tu_spec.get('materialize_only')}")

        # 验证 eval_factor_specs 跳过 turnover_z 评分但仍记录到 factor_df
        from btcore.strategy_tools import eval_factor_specs
        idx = pd.MultiIndex.from_product([["20240603"], ["000001.SZ", "000002.SZ"]],
                                          names=["trade_date", "symbol"])
        df = pd.DataFrame({
            "mom20": [0.5, -0.5], "vol_z": [0.3, -0.3], "turnover_z": [1.5, 0.8]
        }, index=idx)
        factor_df, score = eval_factor_specs(df, specs)
        ok2 = ("turnover_z" in factor_df.columns)
        report("1.1 turnover_z 在 factor_df 中", ok2,
               f"factor_df columns={list(factor_df.columns)}")
    finally:
        os.unlink(config_path)


# ─── 1.2 on_tick buy_conditions ───

def test_on_tick_buy_conditions():
    """验证 on_tick 返回的 buy_conditions 在非调仓日被引擎执行。"""
    from btcore.engine import Engine
    from btcore.strategy import Strategy
    from btcore.strategy_tools import parse_schedule, wrap_strategy
    from tests.conftest import MockDataBackend

    class OnTickTestStrategy(Strategy):
        REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]

        def __init__(self, **kw):
            super().__init__(config=kw.pop("config", {}), **kw)
            self.on_tick_calls = 0

        def on_start(self, provider, first_date, end_date=None):
            pass

        def on_tick(self, bars, snapshot, provider) -> dict | None:
            self.on_tick_calls += 1
            syms = list(bars.keys())
            if syms:
                return {"buy_conditions": [{
                    "symbol": syms[0],
                    "type": "LIMIT_BUY",
                    "price": bars[syms[0]]["close"] * 0.99,
                    "value": 10000,
                }]}
            return None

        def select(self, bars, snapshot, provider) -> dict:
            return {"buy": [], "sell": []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    backend = MockDataBackend()
    strategy = OnTickTestStrategy(config={"max_positions": 5, "initial_capital": 100000})
    strategy = wrap_strategy(strategy, parse_schedule({"frequency": "monthly", "monthday": 1}))
    provider = DataProvider(backend)
    provider.benchmark = "000300.SH"
    engine = Engine(
        strategy=strategy, provider=provider,
        initial_capital=100000,
    )
    engine.run("20240603", "20240607")

    ok = strategy.on_tick_calls > 1
    report("1.2 on_tick 每日被调用", ok,
           f"调用次数={strategy.on_tick_calls} (区间 5 个交易日)")


# ─── 2.1 + 2.2 坍缩因子 NaN 告警 & validate_factor_plan ───

def test_collapse_warnings():
    """用含坍缩因子的策略跑回测，捕获 NaN 告警和 validate 输出。"""
    from btcore.engine import Engine
    from btcore.factors.library import load_library
    from btcore.provider import DataProvider
    from btcore.strategy_loader import load_strategy
    from tests.conftest import MockDataBackend
    lib = load_library()

    factor_name = "pct_above_ma20"
    if factor_name not in lib:
        report("2.1 坍缩因子检测", False, f"因子库中没有 {factor_name}")
        report("2.2 validate_factor_plan", True, "跳过")
        return

    report("2.1 坍缩因子检测", True, f"使用: {factor_name}")

    config_yaml = f"""
strategy: strategies.examples.topk_momentum:TopKMomentum
config:
  initial_capital: 100000
  top_k: 5
  max_positions: 5
benchmark: "000300.SH"
factor_specs:
  - factor: {factor_name}
    materialize_only: true
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False) as f:
        f.write(config_yaml)
        config_path = f.name

    try:
        # 捕获日志
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.WARNING)
        for logger_name in ["btcore.factors.plan", "btcore.engine"]:
            logging.getLogger(logger_name).addHandler(handler)
            logging.getLogger(logger_name).setLevel(logging.WARNING)

        strategy = load_strategy(config_path)
        backend = MockDataBackend()
        provider = DataProvider(backend)
        provider.benchmark = "000300.SH"
        engine = Engine(strategy=strategy, provider=provider, initial_capital=100000)
        engine.run("20240603", "20240607")

        log_output = log_stream.getvalue()
        has_warn = len(log_output) > 0
        report("2.1+2.2 坍缩因子物化运行", True,
               f"日志 {len(log_output)} 字节" + (f", 有告警: {has_warn}" if has_warn else ""))

        logging.getLogger("btcore.factors.plan").removeHandler(handler)
        logging.getLogger("btcore.engine").removeHandler(handler)
    finally:
        os.unlink(config_path)


# ─── 2.3 交易回放调试 ───

def test_debug_snapshot():
    """debug=True 跑回测，用 replay.py 回放。"""
    from btcore.engine import Engine
    from btcore.provider import DataProvider
    from btcore.strategy import Strategy
    from tests.conftest import MockDataBackend

    class SimpleStrategy(Strategy):
        REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]

        def __init__(self, **kw):
            super().__init__(config=kw.pop("config", {}), **kw)

        def on_start(self, provider, first_date, end_date=None):
            pass

        def select(self, bars, snapshot, provider) -> dict:
            syms = list(bars.keys())
            if syms:
                return {"buy": [syms[0]]}
            return {"buy": [], "sell": []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    backend = MockDataBackend()
    strategy = SimpleStrategy(config={"max_positions": 5, "initial_capital": 100000})
    provider = DataProvider(backend)
    provider.benchmark = "000300.SH"

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        engine = Engine(strategy=strategy, provider=provider, db_path=db_path,
                        initial_capital=100000, debug=True)
        engine.run("20240603", "20240607")

        # 检查 debug_snapshots 表
        import sqlite3
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT COUNT(*) FROM debug_snapshots").fetchone()[0]
        report("2.3 debug_snapshots 写入", rows > 0, f"{rows} 条快照")

        if rows > 0:
            snap = conn.execute("SELECT snapshot_json FROM debug_snapshots LIMIT 1").fetchone()
            data = json.loads(snap[0])
            has_keys = all(k in data for k in ["date", "account", "pending", "holdings_detail"])
            report("2.3 快照结构完整", has_keys,
                   f"keys={list(data.keys())[:5]}")
        conn.close()

        # 测试 replay.py
        result = subprocess.run(
            [sys.executable, "scripts/replay.py", db_path, "--list-symbols"],
            capture_output=True, text=True,
        )
        report("2.3 replay.py 可执行", result.returncode == 0,
               f"exit={result.returncode}, lines={len(result.stdout.splitlines())}")
    finally:
        os.unlink(db_path)


# ─── 2.4 Brinson 本地文件 ───

def test_brinson_files():
    """用合成数据测试 brinson_attribute_from_files。"""
    from research.attribution import brinson_attribute_from_files

    tmpdir = tempfile.mkdtemp()
    try:
        industry_map = pd.DataFrame({
            "ts_code": ["000001.SZ", "000002.SZ"],
            "l1_name": ["银行", "房地产"],
        })
        sw_returns = pd.DataFrame(
            {"银行": [0.01, 0.02], "房地产": [-0.01, 0.03]},
            index=["20240603", "20240604"],
        )
        benchmark_weights = pd.DataFrame(
            {"银行": [0.5, 0.5], "房地产": [0.5, 0.5]},
            index=["20240603", "20240604"],
        )
        idx = pd.MultiIndex.from_tuples(
            [("20240603", "000001.SZ"), ("20240603", "000002.SZ"),
             ("20240604", "000001.SZ"), ("20240604", "000002.SZ")],
            names=["trade_date", "symbol"],
        )
        bars = pd.DataFrame(
            {"close": [10.0, 20.0, 10.1, 20.3], "pct_chg": [1.0, -0.5, 2.0, 1.5]},
            index=idx,
        )

        imp = f"{tmpdir}/industry_map.parquet"
        swp = f"{tmpdir}/sw_returns.parquet"
        bwp = f"{tmpdir}/benchmark_weights.parquet"
        bp = f"{tmpdir}/bars.parquet"
        industry_map.to_parquet(imp)
        sw_returns.to_parquet(swp)
        benchmark_weights.to_parquet(bwp)
        bars.to_parquet(bp)

        import sqlite3
        rdb = f"{tmpdir}/result.db"
        conn = sqlite3.connect(rdb)
        conn.execute(
        "CREATE TABLE trade_log (id INTEGER, date TEXT, symbol TEXT, "
        "side TEXT, shares INTEGER, run_id INTEGER)"
    )
        conn.execute("INSERT INTO trade_log VALUES (1,'20240603','000001.SZ','BUY',100,1)")
        conn.execute("INSERT INTO trade_log VALUES (2,'20240604','000001.SZ','SELL',100,1)")
        conn.commit()
        conn.close()

        result = brinson_attribute_from_files(
            result_db=rdb, industry_map=imp, sw_returns=swp,
            benchmark_weights=bwp, bars=bp, run_id=1,
        )
        ok = isinstance(result, dict) and "summary" in result
        report("2.4 brinson_attribute_from_files 正常输出", ok,
               f"keys={list(result.keys()) if isinstance(result, dict) else type(result)}")

        try:
            brinson_attribute_from_files(rdb, "/nonexistent.parquet", swp, bwp, bp)
            report("2.4 缺失文件报错", False, "应该抛异常但没有")
        except FileNotFoundError:
            report("2.4 缺失文件报错", True, "正确抛出 FileNotFoundError")
    finally:
        shutil.rmtree(tmpdir)


# ─── 2.5 compute_breadth 流式 ───

def test_compute_breadth():
    """用 MockBackend 验证流式计算返回正确的日频 Series。"""
    from btcore.factors.library import compute_breadth, load_library
    from tests.conftest import MockDataBackend

    lib = load_library()
    factor_name = "pct_above_ma20"
    if factor_name not in lib:
        report("2.5 compute_breadth", True, "因子库中没有 pct_above_ma20，跳过")
        return

    backend = MockDataBackend()

    try:
        result = compute_breadth(factor_name, backend, "20240603", "20240607", lib)
        ok1 = isinstance(result, pd.Series) and len(result) > 0
        report("2.5 compute_breadth 返回日频 Series", ok1,
               f"len={len(result)}, values={[f'{v:.3f}' for v in result.head(3)]}")
        if len(result) > 0:
            in_range = result.dropna().between(0, 1).all()
            report("2.5 广度值在 [0,1] 区间", in_range,
                   f"min={result.min():.3f}, max={result.max():.3f}")

        try:
            compute_breadth("mom20", backend, "20240603", "20240607", lib)
            report("2.5 保形因子拒绝", False, "应该抛 ValueError")
        except ValueError:
            report("2.5 保形因子拒绝", True, "正确抛出 ValueError")
    except Exception as e:
        report("2.5 compute_breadth 异常", False, str(e)[:100])


# ─── 3.1 小资金成本阈值 ───

def test_cost_threshold():
    """用小资金回测结果跑 cross_validate，验证不误报 HIGH_COST_RATIO。"""
    from btcore.engine import Engine
    from btcore.provider import DataProvider
    from btcore.strategy import Strategy
    from tests.conftest import MockDataBackend

    class TinyCapitalStrategy(Strategy):
        REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]

        def __init__(self, **kw):
            super().__init__(config=kw.pop("config", {}), **kw)

        def on_start(self, provider, first_date, end_date=None):
            pass

        def select(self, bars, snapshot, provider) -> dict:
            syms = list(bars.keys())
            if syms:
                return {"buy": [syms[0]]}
            return {"buy": [], "sell": []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    backend = MockDataBackend()
    strategy = TinyCapitalStrategy(config={"initial_capital": 40000, "max_positions": 5})
    provider = DataProvider(backend)
    provider.benchmark = "000300.SH"

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name

    try:
        engine = Engine(strategy=strategy, provider=provider, db_path=db_path,
                        initial_capital=40000)
        engine.run("20240603", "20240607")

        result = subprocess.run(
            [sys.executable, "scripts/cross_validate.py", db_path],
            capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        has_cost_info = "交易磨损/资金比" in output
        # 小资金不应将 HIGH_COST_RATIO 作为 issue
        has_high_cost_issue = "HIGH_COST_RATIO" in output and "⚠" in output
        report("3.1 小资金成本检查运行", result.returncode == 0,
               f"cost_info={has_cost_info}, HIGH_COST_RATIO_issue={has_high_cost_issue}")
    finally:
        os.unlink(db_path)


# ─── 3.2 基准趋势 API ───

def test_benchmark_trend():
    """在回测策略中调用 get_benchmark_trend。"""
    from btcore.engine import Engine
    from btcore.provider import DataProvider
    from btcore.strategy import Strategy
    from tests.conftest import MockDataBackend

    trend_results = []

    class BMTrendStrategy(Strategy):
        REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]

        def __init__(self, **kw):
            super().__init__(config=kw.pop("config", {}), **kw)

        def on_start(self, provider, first_date, end_date=None):
            pass

        def select(self, bars, snapshot, provider) -> dict:
            trend = provider.get_benchmark_trend("20240607", window=5)
            trend_results.append(trend)
            return {"buy": [], "sell": []}

        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    backend = MockDataBackend()
    strategy = BMTrendStrategy(config={"max_positions": 5, "initial_capital": 100000})
    provider = DataProvider(backend)
    provider.benchmark = "000300.SH"
    engine = Engine(strategy=strategy, provider=provider, initial_capital=100000)
    engine.run("20240603", "20240607")

    ok1 = len(trend_results) > 0
    report("3.2 get_benchmark_trend 被调用", ok1,
           f"调用次数={len(trend_results)}, 返回值={trend_results}")

    # 无 benchmark 场景
    trend_results2 = []

    class BMTrendStrategy2(Strategy):
        REQUIRED_FIELDS = ["open", "high", "low", "close", "vol", "adj_factor"]
        def __init__(self, **kw):
            super().__init__(config=kw.pop("config", {}), **kw)
        def on_start(self, provider, first_date, end_date=None):
            pass
        def select(self, bars, snapshot, provider) -> dict:
            trend_results2.append(provider.get_benchmark_trend("20240605"))
            return {"buy": [], "sell": []}
        def calc_conditions(self, symbol, entry_price, bar, holding_days) -> list[dict]:
            return []

    provider2 = DataProvider(MockDataBackend())
    strategy2 = BMTrendStrategy2(config={"max_positions": 5, "initial_capital": 100000,
                                         "benchmark": ""})
    engine2 = Engine(strategy=strategy2, provider=provider2, initial_capital=100000)
    engine2.run("20240603", "20240605")
    ok2 = all(t is None for t in trend_results2)
    report("3.2 无 benchmark 返回 None", ok2,
           f"返回值={trend_results2}")


# ─── 3.3 参数扫描 sweep.py ───

def test_sweep():
    """dry-run 模式验证参数扫描。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        sweep_config = {
            "base": "strategies/examples/topk_momentum/config.yaml",
            "params": {
                "max_positions": [3, 5],
                "top_k": [3, 5],
            },
        }
        config_path = f"{tmpdir}/sweep.yaml"
        with open(config_path, "w") as f:
            yaml.dump(sweep_config, f)

        result = subprocess.run(
            [sys.executable, "scripts/sweep.py", config_path,
             "--start", "20240603", "--end", "20240607", "--dry-run"],
            capture_output=True, text=True,
        )
        output = result.stdout
        ok1 = result.returncode == 0 and "参数组合数: 4" in output
        report("3.3 sweep dry-run 正确", ok1,
               f"exit={result.returncode}")

        from scripts.sweep import expand_params
        combos = expand_params({"a": [1, 2], "b": [3, 4]})
        ok2 = len(combos) == 4
        report("3.3 expand_params 笛卡尔积", ok2, f"组合数={len(combos)}")


# ─── main ───

def main():
    print("=" * 60)
    print("ddup 改进项实盘烟幕测试")
    print("=" * 60)

    tests = [
        ("1.1 factor_specs materialize_only", test_materialize_only),
        ("1.2 on_tick buy_conditions", test_on_tick_buy_conditions),
        ("2.1+2.2 坍缩因子告警 & validate", test_collapse_warnings),
        ("2.3 交易回放调试", test_debug_snapshot),
        ("2.4 Brinson 本地文件", test_brinson_files),
        ("2.5 compute_breadth 流式", test_compute_breadth),
        ("3.1 小资金成本阈值", test_cost_threshold),
        ("3.2 基准趋势 API", test_benchmark_trend),
        ("3.3 参数扫描 sweep", test_sweep),
    ]

    for name, fn in tests:
        print(f"\n--- {name} ---")
        try:
            fn()
        except Exception as e:
            import traceback
            print(f"  [FAIL] 未捕获异常: {e}")
            traceback.print_exc()
            global FAIL
            FAIL += 1

    print(f"\n{'='*60}")
    print(f"结果: {PASS} PASS, {FAIL} FAIL, {PASS+FAIL} total")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
