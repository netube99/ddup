"""board_factor_eval.py CLI 回归（F-CLI-05）。

idx_ret 因子族（alpha_mom5 等）此前在 board 脚本必然失败且为裸
traceback：无 --benchmark 参数、ensure_pseudo_columns/compute_breadth
缺 benchmark 实参、--decay 非数字裸 ValueError。口径宣称与 factor_eval
同源，错误通道也应对齐。
"""

import sys

import numpy as np
import pandas as pd

import scripts.board_factor_eval as board

# 30 个连续日期：前 10 天充 roc/sum warmup，评估窗从 20240613 起保证因子非 NaN
_DATES = [d.strftime("%Y%m%d") for d in pd.date_range("20240603", periods=30)]
_SYMBOLS = ["600000.SH", "600036.SH", "000001.SZ", "600519.SH", "000333.SZ"]


class _StubBackend:
    """按请求列返回合成面板；get_benchmark_bars 提供日线 hfq_close。"""

    def query_bars(self, symbols, start, end, columns=None):
        cols = list(columns) if columns else ["close", "adj_factor"]
        idx = pd.MultiIndex.from_product(
            [_DATES, _SYMBOLS], names=["trade_date", "symbol"])
        rng = np.random.default_rng(7)
        n = len(idx)
        close = pd.Series(rng.uniform(10, 50, n), index=idx)
        data = {}
        for c in cols:
            if c == "adj_factor":
                data[c] = np.ones(n)
            elif c == "pre_close":
                data[c] = close.groupby("symbol").shift(1).fillna(close)
            elif c in ("open", "high", "low"):
                data[c] = close * rng.uniform(0.98, 1.02, n)
            elif c == "total_mv":
                data[c] = rng.uniform(1e9, 1e11, n)
            else:
                data[c] = close if c == "close" else rng.uniform(1, 100, n)
        df = pd.DataFrame(data, index=idx)
        if symbols is None:
            return df
        return df[df.index.get_level_values("symbol").isin(symbols)]

    def get_benchmark_bars(self, code, start, end):
        dates = [d for d in _DATES if start <= d <= end]
        return pd.DataFrame(
            {"hfq_close": np.linspace(100.0, 130.0, len(dates))},
            index=pd.Index(dates),
        )

    def close(self):
        pass


def _run(monkeypatch, argv):
    monkeypatch.setattr(board.cli_common, "make_provider",
                        lambda: type("P", (), {"backend": _StubBackend()})())
    monkeypatch.setattr(sys, "argv", ["board_factor_eval.py", *argv])
    return board.main()


def test_idx_ret_factor_with_benchmark_succeeds(monkeypatch, capsys):
    """F-CLI-05：--benchmark 传入后 alpha_mom5（roc - sum(idx_ret)）可评估。"""
    rc = _run(monkeypatch, [
        "alpha_mom5", "--start", "20240613", "--end", _DATES[-1],
        "--benchmark", "000300.SH",
    ])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "Traceback" not in out
    assert "RankIC" in out


def test_idx_ret_factor_without_benchmark_clean_error(monkeypatch, capsys):
    """缺 --benchmark：走可读错误通道 exit 1，不再裸 ValueError traceback。"""
    rc = _run(monkeypatch, [
        "alpha_mom5", "--start", "20240613", "--end", _DATES[-1],
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "idx_ret 需要 benchmark" in captured.err
    assert "Traceback" not in captured.err


def test_decay_non_numeric_clean_error(monkeypatch, capsys):
    """F-CLI-05：--decay abc 报可读错误 exit 1，不裸 int() traceback。"""
    rc = _run(monkeypatch, [
        "mom20", "--start", "20240613", "--end", _DATES[-1],
        "--decay", "abc",
    ])
    captured = capsys.readouterr()
    assert rc == 1
    assert "--decay" in captured.err
    assert "Traceback" not in captured.err
