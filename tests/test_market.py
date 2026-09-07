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


from unittest.mock import MagicMock, patch

import pandas as pd

from investment.market import MarketDataError, fetch_fundamentals


def _ticker_stub(info: dict, balance: pd.DataFrame | None = None) -> MagicMock:
    t = MagicMock()
    t.info = info
    t.balance_sheet = balance if balance is not None else pd.DataFrame()
    return t


def test_fetch_fundamentals_maps_fields():
    info = {
        "shortName": "PKSHA TECHNOLOGY INC",
        "marketCap": 98_297_651_200,
        "revenueGrowth": 0.811,
        "operatingMargins": 0.13829,
        "returnOnEquity": 0.07741,
    }
    balance = pd.DataFrame(
        {"2026-03-31": [54_368_529_000, 34_483_425_000]},
        index=["Total Assets", "Stockholders Equity"],
    )
    with patch("investment.market.yf.Ticker", return_value=_ticker_stub(info, balance)):
        f = fetch_fundamentals("3993")

    assert f.symbol == "3993.T"
    assert f.name == "PKSHA TECHNOLOGY INC"
    assert f.market_cap == 98_297_651_200
    assert f.revenue_growth == 0.811
    assert f.roe == 0.07741
    assert f.equity_ratio == pytest.approx(0.6343, abs=0.0005)


def test_fetch_fundamentals_returns_none_for_missing_fields():
    with patch("investment.market.yf.Ticker", return_value=_ticker_stub({})):
        f = fetch_fundamentals("9999")

    assert f.market_cap is None
    assert f.roe is None
    assert f.equity_ratio is None


def test_fetch_fundamentals_raises_on_network_failure():
    with patch("investment.market.yf.Ticker", side_effect=OSError("network down")):
        with pytest.raises(MarketDataError):
            fetch_fundamentals("7203")
