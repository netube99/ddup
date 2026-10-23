"""架构评估探针：只使用合成行情、内存账本和临时结果库。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from btcore import database  # noqa: E402
from btcore.backend import DataBackend  # noqa: E402
from btcore.engine import Engine  # noqa: E402
from btcore.factors import library as factor_library  # noqa: E402
from btcore.provider import DataProvider  # noqa: E402
from btcore.strategy import Strategy  # noqa: E402
from btcore.strategy_loader import build_strategy  # noqa: E402
from research.live import LedgerStore, run_signal  # noqa: E402

SYMBOL = "000001.SZ"
DAYS = ["20240603", "20240604", "20240605"]


class SyntheticBackend(DataBackend):
    def __init__(self, days: list[str], missing: set[str] | None = None):
        self.days = days
        self.missing = missing or set()

    def get_calendar(self, start: str, end: str) -> list[str]:
        return [d for d in self.days if start <= d <= end]

    def get_dividends_on_date(self, date_str: str) -> dict:
        return {}

    def query_bars(
        self, symbols: list[str] | None, start: str, end: str,
        columns: list[str] | None = None,
    ) -> pd.DataFrame:
        dates = [d for d in self.days if start <= d <= end and d not in self.missing]
        if symbols is not None and SYMBOL not in symbols:
            dates = []
        index = pd.MultiIndex.from_tuples(
            [(d, SYMBOL) for d in dates], names=["trade_date", "symbol"],
        )
        values = {
            "open": 10.0, "high": 10.0, "low": 10.0, "close": 10.0,
            "vol": 1000.0, "adj_factor": 1.0, "pre_close": 10.0,
            "up_limit": 11.0, "down_limit": 9.0,
        }
        frame = pd.DataFrame({k: [v] * len(index) for k, v in values.items()}, index=index)
        return frame if columns is None else frame[columns]


class AgeExit(Strategy):
    def select(self, bars: dict, snapshot, provider) -> dict:
        return {
            "sell": [s for s, h in snapshot.holdings.items() if h.holding_days >= 3],
        }


def live_gap() -> dict:
    store = LedgerStore(":memory:")
    try:
        store.init_account(DAYS[0], 9000.0, 10000.0)
        store.append_fill(
            DAYS[0], SYMBOL, "OPENING", price=10.0, shares=100,
            created_at="2026-10-19T00:00:00",
        )
        engine, calendar = run_signal(
            AgeExit(config={"benchmark": ""}),
            DataProvider(SyntheticBackend(DAYS, {DAYS[1]})), store, DAYS[-1],
        )
        actual_days = engine.account.holdings[SYMBOL].holding_days
        actual_sell = engine.pending_actions["sell"]
        return {
            "calendar": calendar,
            "actual_holding_days": actual_days, "expected_holding_days": 3,
            "actual_sell": actual_sell, "expected_sell": [SYMBOL],
            "contract_satisfied": actual_days == 3 and actual_sell == [SYMBOL],
        }
    finally:
        store.close()


def run_identity() -> dict:
    saved = []
    resolved = []
    with tempfile.TemporaryDirectory(prefix="ddup-architecture-") as directory:
        for weight in (1.0, 2.0):
            strategy = build_strategy(
                AgeExit, {"benchmark": ""},
                factor_specs=[{"factor": "probe", "weight": weight}],
                factor_library={"probe": {"expr": "close_hfq"}},
            )
            path = str(Path(directory) / f"run-{weight}.duckdb")
            engine = Engine(strategy, DataProvider(SyntheticBackend(DAYS)), db_path=path)
            engine.run(DAYS[0], DAYS[-1])
            conn = database.connect_result_db(path, read_only=True)
            try:
                saved.append(json.loads(conn.execute("SELECT config_json FROM runs").fetchone()[0]))
            finally:
                conn.close()
            resolved.append(strategy.FACTOR_SPECS[0]["weight"])
    manifests = [config.get("_run_manifest") for config in saved]
    recorded_weights = [
        manifest["factor_specs"][0]["weight"] if manifest else None
        for manifest in manifests
    ]
    return {
        "resolved_weights": resolved,
        "saved_config_equal": saved[0] == saved[1],
        "saved_config_keys": sorted(saved[0]),
        "desired_identity_distinguishes_specs": True,
        "recorded_weights": recorded_weights,
        "contract_satisfied": recorded_weights == resolved and saved[0] != saved[1],
    }


def breadth_work() -> dict:
    dates = pd.bdate_range("2024-06-03", periods=12).strftime("%Y%m%d").tolist()
    sizes = []
    compute = factor_library.compute_factors

    def observe(names: list[str], frame: pd.DataFrame, lib: dict) -> pd.DataFrame:
        sizes.append(len(frame))
        return compute(names, frame, lib)

    with patch.object(factor_library, "compute_factors", side_effect=observe):
        result = factor_library.compute_breadth(
            "probe", SyntheticBackend(dates), {"probe": {"expr": "mean(close_hfq)"}},
            dates[0], dates[-1], chunk_days=4,
        )
    return {
        "unique_input_rows": 12, "materialized_rows_per_chunk": sizes,
        "total_materialized_rows": sum(sizes),
        "all_daily_values_equal_expected_10": bool((result == 10.0).all()),
    }


if __name__ == "__main__":
    print(json.dumps({
        "live_gap": live_gap(), "run_identity": run_identity(), "breadth_work": breadth_work(),
    }, ensure_ascii=False, indent=2))
