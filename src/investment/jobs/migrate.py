"""データベースの構造を最新にする。

migrations/ の .sql をファイル名の順に全部あてる。何度実行しても安全。

## なぜ独立したジョブにしているか

これまで apply_migrations() を呼ぶのはテストだけだった。最初のテーブルは
手作業で作ったため動いていたが、あとから列を足したとき（v3 で bucket 列を
追加したとき）、その変更が本番に一度も適用されないまま
「そんな列は無い」というエラーで止まった。

構造の更新は、データを読み書きするどのジョブよりも先に、必ず行う。
"""

import sys

from investment.db import MIGRATIONS_DIR, apply_migrations, connect


def main() -> int:
    files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    print(f"{len(files)} 件のマイグレーションをあてます: {', '.join(files)}")
    with connect() as conn:
        apply_migrations(conn)
    print("完了しました")
    return 0


if __name__ == "__main__":
    sys.exit(main())
