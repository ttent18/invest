"""仮想ポートフォリオの現金残高を初期化する。

SETTINGS.total_capital を JPY の初期残高として cash テーブルに入れる。
USD は 0 で初期化する。何度実行しても安全（冪等）。金額の出所は
investment.config.SETTINGS.total_capital のみとし、ここでは直書きしない。
"""

import sys

from investment.config import SETTINGS
from investment.db import connect, init_cash, select_cash


def main() -> int:
    with connect() as conn:
        inserted = init_cash(conn, jpy=SETTINGS.total_capital)
        cash = select_cash(conn)

    if inserted:
        print(f"現金残高を初期化しました（新規 {inserted} 件）: {cash}")
    else:
        print(f"現金残高は既に初期化済みのため、何もしませんでした: {cash}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
