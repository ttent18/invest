"""株価と財務データの取得。判定は行わない。"""

from dataclasses import dataclass


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
