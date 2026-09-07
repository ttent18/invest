"""週1回、全銘柄のファンダメンタルズを取得して Neon に保存する。

財務データは四半期に一度しか変わらないため、毎日実行する必要はない。
1銘柄あたり約0.75秒かかるので、銘柄数に比例して時間が伸びる。
"""

import sys
import time
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from investment.db import connect, record_gap, upsert_fundamentals
from investment.market import Fundamentals, MarketDataError, fetch_fundamentals

SYMBOLS_JP = Path(__file__).resolve().parents[3] / "data" / "symbols_jp.txt"
JST = ZoneInfo("Asia/Tokyo")  # 日本株の「その日」は日本時間で決める

# 書き込みを許可する最低の取得成功率。
#
# 通信が一時的に不安定で数銘柄だけ取れないのはよくあることなので、それは許容する。
# しかし通信不調でほとんどの銘柄が取れなかった場合にまで書き込んでしまうと、
# 前回の（正常に取れていた）データが、今日のわずかな件数で上書きされて消えてしまう。
# select_screened は「最新の日付」の行だけを候補にするため、
# 一度上書きされると前回の完全なデータは見えなくなる。
# それを防ぐため、9割以上取得できたときだけ書き込むことにする。
SUCCESS_RATE_THRESHOLD = 0.9


def load_symbols(path: Path) -> list[str]:
    """1行1コードのファイルを読む。空行と # で始まる行は無視する。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def run(symbols: list[str], conn, as_of: date) -> tuple[int, int, bool]:
    """全銘柄を取得する。戻り値は (取得成功件数, 失敗件数, 書き込みを行ったか)。

    1銘柄の失敗で全体を止めない。失敗は data_gaps に記録する。
    取得成功率が SUCCESS_RATE_THRESHOLD 未満の場合は、通信不調で
    ほとんど取れなかった可能性が高いため、書き込み自体を見送る。
    """
    fetched: list[Fundamentals] = []
    failed = 0

    for i, code in enumerate(symbols, 1):
        try:
            fetched.append(fetch_fundamentals(code))
        except MarketDataError as exc:
            failed += 1
            record_gap(conn, scope=f"fundamentals:{code}", detail=str(exc))
        if i % 50 == 0:
            print(f"  {i}/{len(symbols)} 件処理しました", flush=True)
        time.sleep(0.1)  # 連続アクセスを避ける

    ok = len(fetched)
    total = len(symbols)
    success_rate = ok / total if total else 0.0

    if success_rate < SUCCESS_RATE_THRESHOLD:
        record_gap(
            conn,
            scope="fundamentals:batch",
            detail=(
                f"取得成功率が低いため書き込みを見送りました "
                f"(成功 {ok}/{total} = {success_rate:.0%})"
            ),
        )
        return ok, failed, False

    if fetched:
        upsert_fundamentals(conn, fetched, as_of)
    return ok, failed, True


def main() -> int:
    symbols = load_symbols(SYMBOLS_JP)
    print(f"{len(symbols)} 銘柄のファンダメンタルズを取得します")
    started = time.time()
    with connect() as conn:
        ok, failed, written = run(symbols, conn, datetime.now(tz=JST).date())
    elapsed = time.time() - started
    print(f"取得成功 {ok} 件 / 失敗 {failed} 件 / {elapsed:.0f} 秒")
    if written:
        print("保存しました")
        return 0
    print("取得成功率が低かったため、保存を見送りました")
    return 1


if __name__ == "__main__":
    sys.exit(main())
