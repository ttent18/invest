import pytest

from investment.market import is_japanese, normalize_symbol


@pytest.mark.parametrize(
    "given,expected",
    [
        ("7203", "7203.T"),      # 日本株の4桁コード
        ("7203.T", "7203.T"),    # すでに正規化済み
        (" 7203 ", "7203.T"),    # 前後の空白
        ("130A", "130A.T"),      # 2024年以降の英文字入りコード
        ("AAPL", "AAPL"),        # 米国株
        ("aapl", "AAPL"),        # 小文字
        ("BRK-B", "BRK-B"),      # ハイフンを含む米国株
    ],
)
def test_normalize_symbol(given, expected):
    assert normalize_symbol(given) == expected


@pytest.mark.parametrize(
    "symbol,expected",
    [("7203.T", True), ("130A.T", True), ("AAPL", False), ("BRK-B", False)],
)
def test_is_japanese(symbol, expected):
    assert is_japanese(symbol) is expected


def test_normalize_symbol_rejects_empty():
    with pytest.raises(ValueError):
        normalize_symbol("   ")


from investment.market import Fundamentals


def test_fundamentals_holds_optional_values():
    f = Fundamentals(
        symbol="9999.T",
        name="テスト",
        market_cap=None,
        revenue_growth=None,
        operating_margin=None,
        roe=None,
        equity_ratio=None,
    )
    assert f.symbol == "9999.T"
    assert f.market_cap is None
