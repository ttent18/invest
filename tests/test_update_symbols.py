"""scripts/update_symbols.py のテスト。

ネットワークに依存させないため、xlsx をダウンロードする処理は対象にしない。
ダウンロード後の DataFrame を受け取ってフィルタする部分だけを検証する。
"""

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd
from update_symbols import (
    MARKET_COLUMN,
    build_symbols_file,
    extract_domestic_codes,
)

from investment.jobs.run_screen import load_symbols

JST = ZoneInfo("Asia/Tokyo")


def _row(code, market):
    return {
        "日付": 20260831,
        "コード": code,
        "銘柄名": "テスト銘柄",
        MARKET_COLUMN: market,
        "33業種コード": "1",
        "33業種区分": "水産・農林業",
        "17業種コード": "1",
        "17業種区分": "食品",
        "規模コード": "6",
        "規模区分": "TOPIX Small 1",
    }


def _df(rows: list[dict]) -> pd.DataFrame:
    return pd.DataFrame(rows)


def test_extract_domestic_codes_includes_three_domestic_segments():
    df = _df(
        [
            _row(1001, "プライム（内国株式）"),
            _row(2002, "スタンダード（内国株式）"),
            _row(3003, "グロース（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["1001", "2002", "3003"]


def test_extract_domestic_codes_excludes_etf_etn():
    # 1305/1306 (ETF) が混ざらないことの回帰確認
    df = _df(
        [
            _row(1305, "ETF・ETN"),
            _row(1306, "ETF・ETN"),
            _row(1301, "プライム（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["1301"]


def test_extract_domestic_codes_excludes_reit():
    df = _df(
        [
            _row(8951, "REIT・ベンチャーファンド・カントリーファンド・インフラファンド"),
            _row(1301, "プライム（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["1301"]


def test_extract_domestic_codes_excludes_pro_market():
    df = _df(
        [
            _row(9999, "PRO Market"),
            _row(1301, "プライム（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["1301"]


def test_extract_domestic_codes_excludes_foreign_stocks_and_shusshi():
    df = _df(
        [
            _row(1001, "プライム（外国株式）"),
            _row(1002, "スタンダード（外国株式）"),
            _row(1003, "グロース（外国株式）"),
            _row(1004, "出資証券"),
            _row(1301, "プライム（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["1301"]


def test_extract_domestic_codes_handles_alphanumeric_code():
    # 2024年以降は英字を含むコードがある(例: 130A)。int と str が混在するので、
    # どちらも文字列として正しく扱えることを確認する。
    df = _df(
        [
            _row("130A", "プライム（内国株式）"),
            _row(1301, "プライム（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["130A", "1301"]


def test_build_symbols_file_header_lines_are_comments_load_symbols_ignores(tmp_path):
    df = _df([_row(1301, "プライム（内国株式）"), _row(1305, "ETF・ETN")])
    generated_at = datetime(2026, 9, 7, 12, 0, tzinfo=JST)
    text = build_symbols_file(df, source_date="20260831", generated_at=generated_at)

    p = tmp_path / "symbols_jp.txt"
    p.write_text(text, encoding="utf-8")
    symbols = load_symbols(p)

    assert symbols == ["1301"]


def test_build_symbols_file_header_mentions_url_date_and_count():
    df = _df([_row(1301, "プライム（内国株式）")])
    generated_at = datetime(2026, 9, 7, 12, 0, tzinfo=JST)
    text = build_symbols_file(df, source_date="20260831", generated_at=generated_at)

    assert "https://www.jpx.co.jp" in text
    assert "20260831" in text
    assert "1" in text  # 件数
    assert "2026-09-07" in text  # 生成日時


def test_extract_domestic_codes_excludes_five_character_preferred_shares():
    """5文字のコードは優先株式・種類株式なので除外する。

    JPXの一覧では「プライム（内国株式）」に含まれているが、普通株式ではない。
    例: 25935 = 伊藤園第1種優先株式、50765 = インフロニアHD 第1回社債型種類株式。
    株価データの提供元(yfinance)にもデータが無く、取得すると必ず失敗する。
    """
    df = _df(
        [
            _row(25935, "プライム（内国株式）"),
            _row(50765, "プライム（内国株式）"),
            _row(1301, "プライム（内国株式）"),
        ]
    )
    assert extract_domestic_codes(df) == ["1301"]
