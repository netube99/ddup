import pandas as pd
import pytest

from btcore.factors.expr import evaluate_expr, extract_expr_names, validate_expr


def test_simple_expr():
    df = pd.DataFrame({"close": [10.0, 20.0, 5.0]}, index=["A", "B", "C"])
    result = evaluate_expr(df, "1 / close")
    assert result["A"] == 0.1
    assert result["B"] == 0.05
    assert result["C"] == 0.2


def test_expr_with_where():
    df = pd.DataFrame(
        {"pe_ttm": [10.0, 0.0, -5.0, 20.0]},
        index=["A", "B", "C", "D"],
    )
    result = evaluate_expr(df, "1 / pe_ttm", where="pe_ttm > 0")
    assert "A" in result.index
    assert "B" not in result.index
    assert "C" not in result.index
    assert "D" in result.index


def test_validate_rejects_call():
    with pytest.raises(ValueError):
        validate_expr("__import__('os').system('ls')")


def test_validate_rejects_attribute():
    with pytest.raises(ValueError):
        validate_expr("foo.bar")


def test_validate_accepts_arithmetic():
    validate_expr("1 / close + pe_ttm * 2 - 3")


def test_validate_accepts_comparison():
    validate_expr("pe_ttm > 0")


def test_extract_expr_names():
    names = extract_expr_names("close + pe_ttm * 2 - amount / vol")
    assert names == {"close", "pe_ttm", "amount", "vol"}
