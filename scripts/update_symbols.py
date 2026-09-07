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
    """内国株式3市場(プライム/スタンダード/グロース)の銘柄コードだけを抜き出す。

    ネットワークに依存させないため、xlsx を読み込んだ後の DataFrame を
    受け取る形にしてある。ダウンロード処理そのもの (download()) はここでは
    テストしない。
    """
    filtered = df[df[MARKET_COLUMN].isin(INCLUDED_MARKET_SEGMENTS)]
    return [_format_code(v) for v in filtered[CODE_COLUMN].tolist()]


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
        "# 対象: 内国株式のプライム・スタンダード・グロース市場のみ",
        "#   (ETF・ETN、REIT等、PRO Market、外国株式、出資証券は除外)",
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
    codes = extract_domestic_codes(df)

    OUTPUT.write_text(text, encoding="utf-8")
    print(f"{len(codes)} 銘柄を {OUTPUT} に書き出しました（元データの日付: {source_date}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
