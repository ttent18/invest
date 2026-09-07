"""JPX が公開する「東証上場銘柄一覧」から国内株式の銘柄コード一覧を作る。

data/symbols_jp.txt はスクリーニングの母集団になる。以前はここに10銘柄
(しかも大型株ばかり)しか入っておらず、時価総額300億円以下という
スクリーニング条件をほぼ通らなかった。このスクリプトは JPX の公式データ
から国内株式(プライム/スタンダード/グロース)を全件抜き出して置き換える。

## 実行方法

    export PATH="$HOME/.local/bin:$PATH"   # uv が PATH に無い場合
    uv run python scripts/update_symbols.py

JPXのファイルは月次更新なので、毎回のバッチ実行時に取得する必要はない。
生成物 (data/symbols_jp.txt) はリポジトリにコミットする。理由:
- 週次バッチ (run_screen.py) が外部ファイルの取得失敗で止まらないようにする
- 何を対象にしているかを git の履歴で追えるようにする
- JPXのファイルは月次更新であり、毎回取りに行く必要が無い

更新したくなったら(月次程度を目安に)、このスクリプトを再実行して
差分をコミットすること。
"""

import io
import sys
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

JPX_URL = (
    "https://www.jpx.co.jp/markets/statistics-equities/misc/"
    "tvdivq0000001vg2-att/data_j.xlsx"
)
OUTPUT = Path(__file__).resolve().parents[1] / "data" / "symbols_jp.txt"
JST = ZoneInfo("Asia/Tokyo")

CODE_COLUMN = "コード"
MARKET_COLUMN = "市場・商品区分"
DATE_COLUMN = "日付"

# 含める市場・商品区分: 個別の内国企業の株式のみ。
#
# 除外する区分とその理由:
# - ETF・ETN / REIT・ベンチャーファンド・カントリーファンド・インフラファンド:
#   個別企業ではないため、財務指標(売上高増減率・ROE等)によるスクリーニングが
#   意味を持たない
# - PRO Market: 特定投資家向けの市場であり、個人は売買できない
# - プライム/スタンダード/グロース(外国株式)・出資証券: 対象外
INCLUDED_MARKET_SEGMENTS = (
    "プライム（内国株式）",
    "スタンダード（内国株式）",
    "グロース（内国株式）",
)

# 普通株式の銘柄コードの文字数。
#
# 日本株の銘柄コードは4文字（例: 7203、130A）。5文字のコードは
# 優先株式・種類株式で、普通株式とは別物である。
# 例: 25935 = 伊藤園第1種優先株式、50765 = インフロニアHD 第1回社債型種類株式。
#
# これらは JPX の一覧では「プライム（内国株式）」に分類されているため
# 市場区分では除けないが、
# - 会社そのものを買うものではなく、この仕組みが想定している投資対象ではない
# - 株価データの提供元(yfinance)にデータが無く、取得すると必ず失敗する
# ため、文字数で除外する。
COMMON_STOCK_CODE_LENGTH = 4


def download(url: str = JPX_URL) -> bytes:
    """JPXのxlsxをダウンロードする。"""
    with urllib.request.urlopen(url) as resp:
        return resp.read()


def _format_code(value: object) -> str:
    """コード列の値を文字列に整える。

    pandas はコード列を数字だけのコード(例: 1301)は int、英字を含む
    コード(例: 130A、2024年以降に導入)は str として読み込み、型が混在する。
    int をそのまま str() すれば "1301" になり問題は無いが、意図を明示する
    ために両方のケースを分けて書く。
    """
    if isinstance(value, int):
        return str(value)
    return str(value).strip()


def extract_domestic_codes(df: pd.DataFrame) -> list[str]:
    """内国株式3市場(プライム/スタンダード/グロース)の普通株式だけを抜き出す。

    市場区分で絞ったうえで、コードが4文字のもの(普通株式)だけを残す。
    5文字のコードは優先株式・種類株式なので除く(COMMON_STOCK_CODE_LENGTH 参照)。

    ネットワークに依存させないため、xlsx を読み込んだ後の DataFrame を
    受け取る形にしてある。ダウンロード処理そのもの (download()) はここでは
    テストしない。
    """
    kept, _ = split_domestic_codes(df)
    return kept


def split_domestic_codes(df: pd.DataFrame) -> tuple[list[str], list[str]]:
    """内国株式3市場のコードを (残すもの, 除外したもの) に分けて返す。

    除外したものも返すのは、黙って捨てないため。JPX 側のファイル形式が
    変わってコードの見え方が変わった場合（例: 1301 が "1301.0" と読まれる）、
    母集団が気づかないうちに減ってしまう。再生成時に件数を表示して
    気づけるようにする。
    """
    filtered = df[df[MARKET_COLUMN].isin(INCLUDED_MARKET_SEGMENTS)]
    codes = [_format_code(v) for v in filtered[CODE_COLUMN].tolist()]
    kept = [c for c in codes if len(c) == COMMON_STOCK_CODE_LENGTH]
    excluded = [c for c in codes if len(c) != COMMON_STOCK_CODE_LENGTH]
    return kept, excluded


def build_symbols_file(df: pd.DataFrame, source_date: str, generated_at: datetime) -> str:
    """data/symbols_jp.txt に書き出すテキストを組み立てる。

    先頭のコメント行 (# で始まる) は load_symbols() (run_screen.py) が
    読み飛ばす実装になっていることを確認済み。
    """
    codes = extract_domestic_codes(df)
    header = [
        f"# 生成日時: {generated_at.isoformat()}",
        f"# 出所: {JPX_URL}",
        f"# 元データの日付: {source_date}",
        f"# 件数: {len(codes)}",
        "#",
        "# 対象: 内国株式のプライム・スタンダード・グロース市場の普通株式のみ",
        "#   (ETF・ETN、REIT等、PRO Market、外国株式、出資証券は除外)",
        "#   (5文字コードの優先株式・種類株式も除外)",
        "#",
        "# 更新方法: uv run python scripts/update_symbols.py",
        "# (JPXのファイルは月次更新。更新のたびに再実行してコミットすること)",
    ]
    return "\n".join(header + codes) + "\n"


def main() -> int:
    print(f"ダウンロード中: {JPX_URL}")
    raw = download()
    df = pd.read_excel(io.BytesIO(raw))
    source_date = str(df[DATE_COLUMN].iloc[0])
    generated_at = datetime.now(tz=JST)

    text = build_symbols_file(df, source_date, generated_at)
    codes, excluded = split_domestic_codes(df)

    OUTPUT.write_text(text, encoding="utf-8")
    print(f"{len(codes)} 銘柄を {OUTPUT} に書き出しました（元データの日付: {source_date}）")
    if excluded:
        print(
            f"4文字でないコード {len(excluded)} 件を除外しました"
            f"（優先株式・種類株式）: {', '.join(excluded)}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
