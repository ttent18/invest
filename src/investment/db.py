"""Neon (Postgres) への接続とクエリ。

記録の整合性を保つため、複数テーブルにまたがる更新は必ずトランザクションで行う。
"""

import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from investment.config import ScreenCriteria
from investment.market import Fundamentals

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@contextmanager
def connect(url: str | None = None):
    """接続を開く。url を省略した場合は環境変数 DATABASE_URL を使う。"""
    load_dotenv()
    dsn = url or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL が設定されていません")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn


def apply_migrations(conn) -> None:
    """migrations/ の .sql を名前順に適用する。何度実行しても安全。"""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def upsert_fundamentals(conn, rows: list[Fundamentals], as_of: date) -> int:
    """ファンダメンタルズを保存する。同じ日の同じ銘柄は上書きする。"""
    sql = """
        INSERT INTO fundamentals
            (symbol, as_of, name, market_cap, revenue_growth,
             operating_margin, roe, equity_ratio)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, as_of) DO UPDATE SET
            name = EXCLUDED.name,
            market_cap = EXCLUDED.market_cap,
            revenue_growth = EXCLUDED.revenue_growth,
            operating_margin = EXCLUDED.operating_margin,
            roe = EXCLUDED.roe,
            equity_ratio = EXCLUDED.equity_ratio
    """
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    f.symbol, as_of, f.name, f.market_cap, f.revenue_growth,
                    f.operating_margin, f.roe, f.equity_ratio,
                )
                for f in rows
            ],
        )
    conn.commit()
    return len(rows)


def select_screened(conn, criteria: ScreenCriteria, limit: int) -> list[dict]:
    """最新の取得日で、スクリーニング条件を満たす銘柄を返す。

    NULL の項目は通さない。SQL の比較で NULL は偽になるため、
    明示的に IS NOT NULL を書かなくても除外されるが、意図を示すため書く。
    """
    sql = """
        SELECT * FROM fundamentals
        WHERE as_of = (SELECT MAX(as_of) FROM fundamentals)
          AND market_cap       IS NOT NULL AND market_cap       <= %s
          AND revenue_growth   IS NOT NULL AND revenue_growth   >= %s
          AND operating_margin IS NOT NULL AND operating_margin >= %s
          AND roe              IS NOT NULL AND roe              >= %s
          AND equity_ratio     IS NOT NULL AND equity_ratio     >= %s
        ORDER BY revenue_growth DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                criteria.max_market_cap,
                criteria.min_revenue_growth,
                criteria.min_operating_margin,
                criteria.min_roe,
                criteria.min_equity_ratio,
                limit,
            ),
        )
        return list(cur.fetchall())


def record_gaps(conn, gaps: list[tuple[str, str]]) -> int:
    """データが取れなかった事実をまとめて残す。取れたことにしない。

    gaps は (scope, detail) の組のリスト。何千件になっても書き込みは1回で
    済ませる。1件ずつ接続を往復させると、件数が多いときに時間がかかり、
    その間に Neon 側から接続を切られる恐れがあるため。
    """
    if not gaps:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO data_gaps (occurred_at, scope, detail) VALUES (NOW(), %s, %s)",
            gaps,
        )
    conn.commit()
    return len(gaps)


def record_gap(conn, scope: str, detail: str) -> None:
    """データが取れなかった事実を1件残す。取れたことにしない。"""
    record_gaps(conn, [(scope, detail)])


def select_positions(conn) -> list[dict]:
    """現在の保有を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM positions ORDER BY symbol")
        return list(cur.fetchall())


def select_cash(conn) -> dict[str, float]:
    """通貨ごとの現金残高を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT currency, amount FROM cash")
        return {r["currency"]: float(r["amount"]) for r in cur.fetchall()}


def select_capital(conn) -> float:
    """いまの総資金を返す。現金の残高 ＋ 保有の取得原価。

    現在の株価は使わない。理由は2つ。

    1. 現在値を使うと、含み益が出ているだけで次に買う金額が膨らみ、
       リスクが勝手に増えてしまう
    2. 現在値の取得は通信が必要で失敗しうる。総資金の計算が
       通信の失敗で止まるのは筋が悪い

    利確して現金が増えれば総資金も増えるので、
    「利確して資金を増やし、さらに投資する」という循環は成立する。

    いまは円だけを数える。ドルを円に足すと桁が狂うため。
    米国株を有効にするときに、為替を掛けて足す形へ直すこと。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS c FROM cash WHERE currency = 'JPY'")
        cash = float(cur.fetchone()["c"])
        cur.execute(
            """
            SELECT COALESCE(SUM(quantity * avg_price), 0) AS c
            FROM positions WHERE currency = 'JPY'
            """
        )
        held = float(cur.fetchone()["c"])
    return cash + held


def init_cash(conn, jpy: float) -> int:
    """現金残高を初期化する。何度実行しても安全（冪等）。

    JPY を指定額、USD を 0 で初期化する。通貨ごとに ON CONFLICT DO NOTHING を
    使うため、既に残高がある通貨（取引などで変動済みのもの）には一切触れず、
    上書きしない。戻り値は新規に挿入した通貨の件数（0〜2）。
    """
    inserted = 0
    with conn.cursor() as cur:
        for currency, amount in (("JPY", jpy), ("USD", 0)):
            cur.execute(
                """
                INSERT INTO cash (currency, amount) VALUES (%s, %s)
                ON CONFLICT (currency) DO NOTHING
                """,
                (currency, amount),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted
