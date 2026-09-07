"""週1回、全銘柄のファンダメンタルズを取得して Neon に保存する。

財務データは四半期に一度しか変わらないため、毎日実行する必要はない。
1銘柄あたり約0.75秒かかるので、銘柄数に比例して時間が伸びる。
3,700銘柄では50分近くかかる。

## 「取得」と「保存」を完全に分けている理由

Neon(利用しているデータベース)は、数分間アクセスが無いと接続を自動的に
切断する(無料枠の省電力機能)。以前はデータベースに接続したまま50分間の
取得ループを回しており、途中で接続が切れて、次の書き込みでクラッシュした。

これを避けるため、

  1. まず全銘柄を取得する(この間、データベースには一切触れない)
  2. 取得が終わってから接続を開き、まとめて書き出して、すぐ閉じる

という順序にしてある。接続が開いている時間は数秒で済み、切断されない。
"""

import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from investment.db import connect, record_gaps, upsert_fundamentals
from investment.market import Fundamentals, MarketDataError, fetch_fundamentals, has_no_data

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


@dataclass(frozen=True)
class FetchResult:
    """取得ループの結果。まだデータベースには何も書いていない状態を表す。

    gaps は (scope, detail) の組。「取れなかった事実」を後でまとめて
    data_gaps テーブルに記録するために持ち回る。
    """

    fetched: list[Fundamentals] = field(default_factory=list)
    gaps: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> int:
        """取得できた件数。"""
        return len(self.fetched)

    @property
    def failed(self) -> int:
        """取得できなかった件数。失敗1件につき gaps が1件増える。"""
        return len(self.gaps)


def load_symbols(path: Path) -> list[str]:
    """1行1コードのファイルを読む。空行と # で始まる行は無視する。"""
    lines = path.read_text(encoding="utf-8").splitlines()
    return [s for line in lines if (s := line.strip()) and not s.startswith("#")]


def fetch_all(symbols: list[str]) -> FetchResult:
    """全銘柄を取得する。データベースには一切触れない。

    1銘柄の失敗で全体を止めない。失敗した事実は戻り値の gaps に貯めておき、
    保存フェーズ(persist)でまとめて記録する。
    """
    fetched: list[Fundamentals] = []
    gaps: list[tuple[str, str]] = []

    for i, code in enumerate(symbols, 1):
        try:
            f = fetch_fundamentals(code)
        except MarketDataError as exc:
            gaps.append((f"fundamentals:{code}", str(exc)))
        else:
            # fetch_fundamentals は例外を出さずに「全項目 None」の Fundamentals を
            # 返すことがある(yfinance がレート制限等で空の info を返した場合)。
            # これを取得成功として数えると、成功率100%のまま空の行が upsert され、
            # ON CONFLICT で前回の正常なデータを上書きしてしまう。
            # 全項目が None の場合だけを「そもそも取得できていない」失敗として扱う
            # (一部だけ None なのは正常な欠損であり、従来どおり成功として扱う)。
            if has_no_data(f):
                gaps.append(
                    (
                        f"fundamentals:{code}",
                        f"{f.symbol} は全項目が取得できませんでした（空データのため取得失敗として扱います）",
                    )
                )
            else:
                fetched.append(f)
        if i % 50 == 0:
            print(f"  {i}/{len(symbols)} 件処理しました", flush=True)
        time.sleep(0.1)  # 連続アクセスを避ける

    return FetchResult(fetched=fetched, gaps=gaps)


def persist(conn, result: FetchResult, as_of: date) -> bool:
    """取得結果をデータベースに書き出す。戻り値は書き込みを行ったか。

    接続が開いている時間を最短にするため、ここでの書き込みは
    「失敗の記録をまとめて1回」＋「取得できた分をまとめて1回」だけにしてある。
    取得成功率が SUCCESS_RATE_THRESHOLD 未満の場合は、通信不調で
    ほとんど取れなかった可能性が高いため、書き込み自体を見送る。
    """
    total = result.ok + result.failed
    success_rate = result.ok / total if total else 0.0
    gaps = list(result.gaps)

    if success_rate < SUCCESS_RATE_THRESHOLD:
        gaps.append(
            (
                "fundamentals:batch",
                f"取得成功率が低いため書き込みを見送りました (成功 {result.ok}/{total} = {success_rate:.0%})",
            )
        )
        record_gaps(conn, gaps)
        return False

    if gaps:
        record_gaps(conn, gaps)
    if result.fetched:
        upsert_fundamentals(conn, result.fetched, as_of)
    return True


def main() -> int:
    symbols = load_symbols(SYMBOLS_JP)
    print(f"{len(symbols)} 銘柄のファンダメンタルズを取得します")
    started = time.time()

    # 取得を先に終わらせる。ここではまだデータベースに接続しない
    # (接続したまま待たせると Neon 側から切断されるため)。
    result = fetch_all(symbols)
    elapsed = time.time() - started
    print(f"取得成功 {result.ok} 件 / 失敗 {result.failed} 件 / {elapsed:.0f} 秒")

    print("データベースに保存します")
    with connect() as conn:
        written = persist(conn, result, datetime.now(tz=JST).date())

    if written:
        print("保存しました")
        return 0
    print("取得成功率が低かったため、保存を見送りました")
    return 1


if __name__ == "__main__":
    sys.exit(main())
