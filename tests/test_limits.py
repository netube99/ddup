from btcore.limits import get_limit_prices
from tests.conftest import make_bar


def _missing_limits_bar(**kw):
    """up/down_limit 缺失（LEFT JOIN 缺行）→ get_limit_prices 回退板块规则。"""
    return make_bar(up_limit=None, down_limit=None, **kw)


def test_uses_stk_limit_values():
    bar = make_bar(up_limit=11.50, down_limit=9.00)
    up, down = get_limit_prices("000001.SZ", bar, "20240601")
    assert up == 11.50
    assert down == 9.00


def test_fallback_main_board():
    bar = _missing_limits_bar()
    up, down = get_limit_prices("600001.SH", bar, "20240601")
    assert up == 11.00
    assert down == 9.00


def test_fallback_star_board():
    bar = _missing_limits_bar()
    up, down = get_limit_prices("688001.SH", bar, "20240601")
    assert up == 12.00
    assert down == 8.00


def test_fallback_chinext_pre_switch():
    bar = _missing_limits_bar()
    up, down = get_limit_prices("300001.SZ", bar, "20200823")
    assert up == 11.00
    assert down == 9.00


def test_fallback_chinext_post_switch():
    bar = _missing_limits_bar()
    up, down = get_limit_prices("300001.SZ", bar, "20200824")
    assert up == 12.00
    assert down == 8.00


def test_fallback_bj():
    bar = _missing_limits_bar()
    up, down = get_limit_prices("830001.BJ", bar, "20240601")
    assert up == 13.00
    assert down == 7.00


def test_pre_close_zero():
    bar = _missing_limits_bar(pre_close=0.0)
    up, down = get_limit_prices("600001.SH", bar, "20240601")
    assert up is None
    assert down is None
