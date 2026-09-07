"""株価と財務データの取得。判定は行わない。"""

import math
from dataclasses import dataclass
from datetime import date, timedelta

import yfinance as yf


@dataclass(frozen=True)
class Fundamentals:
    """1銘柄の財務指標。取得できなかった項目は None。

    None を 0 で埋めない。取得失敗と「値が0」を区別する必要がある。
    """

    symbol: str
    name: str
    market_cap: float | None
    revenue_growth: float | None
    operating_margin: float | None
    roe: float | None
    equity_ratio: float | None


def has_no_data(f: Fundamentals) -> bool:
    """5つの指標がすべて None かどうかを返す。

    yfinance はレート制限などにあたっても例外を投げず、空の info（{}）を
    静かに返すことがある。この場合 fetch_fundamentals は例外を出さず、
    全項目が None の Fundamentals を返す。呼び出し側がこれを「取得成功」
    と数えてしまうと、成功率100%のまま空の行が upsert され、
    ON CONFLICT によって前回の（正常だった）データを上書きしてしまう。
    一部の項目だけが None なのは正常な欠損（screen.evaluate 側が
    「取得できません」として弾く設計）なので成功として扱ってよいが、
    全項目が None の場合だけは「そもそも取得できていない」状態であり、
    取得失敗として扱う必要がある。
    """
    return (
        f.market_cap is None
        and f.revenue_growth is None
        and f.operating_margin is None
        and f.roe is None
        and f.equity_ratio is None
    )


def normalize_symbol(code: str) -> str:
    """銘柄コードを yfinance が受け取る形に整える。

    日本株は4文字（数字始まり）で、末尾に .T を付ける。
    2024年以降は英文字を含むコードがあるため、数字だけとは限らない。
    """
    s = code.strip().upper()
    if not s:
        raise ValueError("銘柄コードが空です")
    if s.endswith(".T"):
        return s
    if len(s) == 4 and s[0].isdigit() and s.isalnum():
        return f"{s}.T"
    return s


def is_japanese(symbol: str) -> bool:
    """正規化済みのシンボルが日本株かどうかを返す。"""
    return symbol.endswith(".T")


class MarketDataError(RuntimeError):
    """株価・財務データの取得に失敗した。

    このエラーが出た日は判断を行わない。古い値で代用しない。
    """


def _as_float(value) -> float | None:
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f  # NaN を除く


def _equity_ratio(balance_sheet) -> float | None:
    """貸借対照表から自己資本比率を計算する。取れなければ None。

    値が NaN の場合も None にする（NaN は「取得できた」ことにしない）。
    """
    if balance_sheet is None or getattr(balance_sheet, "empty", True):
        return None
    try:
        raw_equity = balance_sheet.loc["Stockholders Equity"].iloc[0]
        raw_total = balance_sheet.loc["Total Assets"].iloc[0]
    except (KeyError, IndexError):
        return None
    equity = _as_float(raw_equity)
    total = _as_float(raw_total)
    if equity is None or total is None or total == 0:
        return None
    return equity / total


def fetch_fundamentals(code: str) -> Fundamentals:
    """1銘柄の財務指標を取得する。

    個別の項目が取れないことは正常(None を入れる)。
    通信そのものが失敗した場合は MarketDataError を投げる。
    """
    symbol = normalize_symbol(code)
    try:
        ticker = yf.Ticker(symbol)
        info = ticker.info or {}
        balance = ticker.balance_sheet
    except Exception as exc:  # yfinance は多様な例外を投げる
        # 元の例外の型名を含めることで、通信失敗とプログラミングエラー（属性名の
        # タイプミス、yfinance の API 変更など）を後から区別できるようにする。
        raise MarketDataError(f"{symbol} の取得に失敗しました ({type(exc).__name__}: {exc})") from exc

    return Fundamentals(
        symbol=symbol,
        name=str(info.get("shortName") or info.get("longName") or symbol),
        market_cap=_as_float(info.get("marketCap")),
        revenue_growth=_as_float(info.get("revenueGrowth")),
        operating_margin=_as_float(info.get("operatingMargins")),
        roe=_as_float(info.get("returnOnEquity")),
        equity_ratio=_equity_ratio(balance),
    )


def fetch_last_price(code: str) -> float:
    """直近の終値（または最終取引価格）を取得する。

    取得できなければ MarketDataError を投げる。0 や None を返さない。
    このエラーが出た銘柄は判断を行わない（古い価格を使わない）。
    """
    symbol = normalize_symbol(code)
    try:
        ticker = yf.Ticker(symbol)
        history = ticker.history(period="5d")
    except Exception as exc:  # yfinance は多様な例外を投げる
        raise MarketDataError(
            f"{symbol} の株価取得に失敗しました ({type(exc).__name__}: {exc})"
        ) from exc

    price = None if history.empty else _as_float(history["Close"].iloc[-1])
    if price is None or price <= 0:
        raise MarketDataError(f"{symbol} の株価が取得できませんでした")
    return price


def fetch_range(symbol: str, start: date, end: date) -> tuple[float, float]:
    """[start, end]（両端含む）の期間内の最高値・最安値を返す。取れなければ MarketDataError。

    yfinance の呼び出しは1回のみ（銘柄あたりの通信回数を抑えるため）。
    単一日の値動きが欲しい場合は start == end を渡せばよい。
    """
    try:
        hist = yf.Ticker(symbol).history(
            start=start.isoformat(), end=(end + timedelta(days=1)).isoformat()
        )
    except Exception as exc:
        raise MarketDataError(f"{symbol} の {start}〜{end} の値動きを取得できません") from exc

    if hist.empty:
        raise MarketDataError(f"{symbol} の {start}〜{end} のデータがありません")

    # 兄弟の fetch_last_price は NaN をガードしているが、こちらは漏れていた。
    # OHLC が NaN の行を例外なしで返すと、detect_hits の nan <= x / nan >= x は
    # 常に False になり、約定の見落としが静かに起きる（例外もエラーも出ない）。
    high = _as_float(hist["High"].max())
    low = _as_float(hist["Low"].min())
    if high is None or low is None:
        raise MarketDataError(f"{symbol} の {start}〜{end} の値動きが不正です（NaN を含みます）")
    return high, low
