"""MockDataBackend 的 aux 表契约测试：moneyflow/cyq_perf/margin_detail
经 LEFT JOIN 并入 bars（镜像 adapters/tushare.py 的 extra_fields 行为）。"""

from tests.conftest import MockDataBackend


def test_aux_fields_visible_in_bars():
    """aux 列在 bars 中可见，策略无需知道列来自哪张表。"""
    backend = MockDataBackend()
    bars = backend.query_bars(None, "20240603", "20240607")

    # 三张 aux 表各挑一个代表列
    for col in ("buy_sm_amount", "winner_rate", "rzye"):
        assert col in bars.columns
        assert bars[col].notna().any()

    # LEFT JOIN 不丢 bars 行（缺行留 NaN）
    plain = backend._bars
    mask = (plain["trade_date"] >= "20240603") & (plain["trade_date"] <= "20240607")
    assert len(bars) == mask.sum()
