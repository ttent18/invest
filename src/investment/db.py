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
