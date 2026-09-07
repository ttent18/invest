import pytest

from investment.market import (
    is_japanese,
    lot_size,
    normalize_symbol,
)


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
    assert f.operating_margin == 0.13829
    assert f.roe == 0.07741
    assert f.equity_ratio == pytest.approx(0.6343, abs=0.0005)


def test_fetch_fundamentals_returns_none_for_missing_fields():
    with patch("investment.market.yf.Ticker", return_value=_ticker_stub({})):
        f = fetch_fundamentals("9999")

    assert f.market_cap is None
    assert f.roe is None
    assert f.equity_ratio is None


def test_fetch_fundamentals_raises_on_network_failure():
    with (
        patch("investment.market.yf.Ticker", side_effect=OSError("network down")),
        pytest.raises(MarketDataError),
    ):
        fetch_fundamentals("7203")


def test_fetch_fundamentals_equity_ratio_none_when_equity_is_nan():
    # 貸借対照表の自己資本が NaN のとき、equity_ratio は None であるべき（0や誤った値を返してはならない）
    balance = pd.DataFrame(
        {"2026-03-31": [54_368_529_000, float("nan")]},
        index=["Total Assets", "Stockholders Equity"],
    )
    with patch("investment.market.yf.Ticker", return_value=_ticker_stub({}, balance)):
        f = fetch_fundamentals("3993")

    assert f.equity_ratio is None


def test_fetch_fundamentals_equity_ratio_none_when_total_assets_is_nan():
    # 総資産が NaN のときも同様に None であるべき
    balance = pd.DataFrame(
        {"2026-03-31": [float("nan"), 34_483_425_000]},
        index=["Total Assets", "Stockholders Equity"],
    )
    with patch("investment.market.yf.Ticker", return_value=_ticker_stub({}, balance)):
        f = fetch_fundamentals("3993")

    assert f.equity_ratio is None


from investment.market import fetch_last_price


def _history_stub(closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"Close": closes})


def test_fetch_last_price_returns_latest_close():
    ticker = MagicMock()
    ticker.history.return_value = _history_stub([2500.0, 2550.5])
    with patch("investment.market.yf.Ticker", return_value=ticker):
        price = fetch_last_price("7203")

    assert price == 2550.5


def test_fetch_last_price_normalizes_symbol():
    ticker = MagicMock()
    ticker.history.return_value = _history_stub([1500.0])
    with patch("investment.market.yf.Ticker", return_value=ticker) as mock_ticker_cls:
        fetch_last_price("7203")

    mock_ticker_cls.assert_called_once_with("7203.T")


def test_fetch_last_price_raises_when_history_is_empty():
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame()
    with (
        patch("investment.market.yf.Ticker", return_value=ticker),
        pytest.raises(MarketDataError),
    ):
        fetch_last_price("7203")


def test_fetch_last_price_raises_when_close_is_nan():
    ticker = MagicMock()
    ticker.history.return_value = _history_stub([float("nan")])
    with (
        patch("investment.market.yf.Ticker", return_value=ticker),
        pytest.raises(MarketDataError),
    ):
        fetch_last_price("7203")


def test_fetch_last_price_raises_on_network_failure():
    with (
        patch("investment.market.yf.Ticker", side_effect=OSError("network down")),
        pytest.raises(MarketDataError),
    ):
        fetch_last_price("7203")


from datetime import date as _date

from investment.market import fetch_range


def _range_stub(highs: list[float], lows: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"High": highs, "Low": lows})


def test_fetch_range_returns_period_high_and_low():
    # 期間内で最高値・最安値が別々の日にあっても正しく拾えること（先頭行だけを見ない）
    ticker = MagicMock()
    ticker.history.return_value = _range_stub(
        highs=[2500.0, 2600.0, 2400.0], lows=[2450.0, 2550.0, 2200.0]
    )
    with patch("investment.market.yf.Ticker", return_value=ticker):
        high, low = fetch_range("7203.T", _date(2026, 9, 1), _date(2026, 9, 3))

    assert high == 2600.0
    assert low == 2200.0


def test_fetch_range_calls_history_once():
    # 銘柄あたりの通信回数を1回に抑える
    ticker = MagicMock()
    ticker.history.return_value = _range_stub([2500.0], [2450.0])
    with patch("investment.market.yf.Ticker", return_value=ticker):
        fetch_range("7203.T", _date(2026, 9, 1), _date(2026, 9, 5))

    ticker.history.assert_called_once()


def test_fetch_range_raises_when_history_is_empty():
    ticker = MagicMock()
    ticker.history.return_value = pd.DataFrame()
    with (
        patch("investment.market.yf.Ticker", return_value=ticker),
        pytest.raises(MarketDataError),
    ):
        fetch_range("7203.T", _date(2026, 9, 1), _date(2026, 9, 3))


def test_fetch_range_raises_when_high_is_nan():
    # I3: 高値が NaN の行を例外なしで返してしまうと、detect_hits の
    # nan <= x / nan >= x は常に False になり、約定の見落としが静かに起きる。
    # 兄弟の fetch_last_price と同じく NaN はガードするべき。
    ticker = MagicMock()
    ticker.history.return_value = _range_stub(
        highs=[float("nan")], lows=[2450.0]
    )
    with (
        patch("investment.market.yf.Ticker", return_value=ticker),
        pytest.raises(MarketDataError),
    ):
        fetch_range("7203.T", _date(2026, 9, 1), _date(2026, 9, 3))


def test_fetch_range_raises_when_low_is_nan():
    ticker = MagicMock()
    ticker.history.return_value = _range_stub(
        highs=[2600.0], lows=[float("nan")]
    )
    with (
        patch("investment.market.yf.Ticker", return_value=ticker),
        pytest.raises(MarketDataError),
    ):
        fetch_range("7203.T", _date(2026, 9, 1), _date(2026, 9, 3))


def test_fetch_range_raises_on_network_failure():
    with (
        patch("investment.market.yf.Ticker", side_effect=OSError("network down")),
        pytest.raises(MarketDataError),
    ):
        fetch_range("7203.T", _date(2026, 9, 1), _date(2026, 9, 3))


# --- 売買単位（単元株） -----------------------------------------------------


def test_lot_size_is_100_for_japanese_stocks():
    """日本株は100株単位でしか売買できない。

    SBI証券には1株から買える「S株」もあるが、成行注文しか出せず逆指値が
    使えない（https://search.sbisec.co.jp/v2/popwin/attention/trading/stock_07.html）。
    このシステムは損切りを証券会社側の逆指値に任せる設計なので、S株は使えない。
    """
    assert lot_size("7203.T") == 100
    assert lot_size("3723.T") == 100


def test_lot_size_is_1_for_us_stocks():
    """米国株は1株から買える。"""
    assert lot_size("AAPL") == 1
    assert lot_size("NVDA") == 1
