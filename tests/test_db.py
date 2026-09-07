import os
from datetime import date

import pytest

from investment.config import SCREEN
from investment.db import apply_migrations, connect, select_screened, upsert_fundamentals
from investment.market import Fundamentals

pytestmark = pytest.mark.integration

TEST_URL = os.environ.get("DATABASE_URL_TEST")


@pytest.fixture
def conn():
    if not TEST_URL:
        pytest.skip("DATABASE_URL_TEST が未設定のためスキップします")
    with connect(TEST_URL) as c:
        apply_migrations(c)
        with c.cursor() as cur:
            cur.execute("TRUNCATE fundamentals, trades, positions, proposals, cash, data_gaps")
        c.commit()
        yield c


def test_apply_migrations_is_idempotent(conn):
    apply_migrations(conn)  # 2回目でも失敗しない
    apply_migrations(conn)


def test_upsert_and_select_screened(conn):
    today = date(2026, 9, 7)
    rows = [
        Fundamentals("1111.T", "通過する会社", 10_000_000_000, 0.30, 0.20, 0.25, 0.60),
        Fundamentals("2222.T", "時価総額が大きい会社", 90_000_000_000, 0.30, 0.20, 0.25, 0.60),
        Fundamentals("3333.T", "ROEが低い会社", 10_000_000_000, 0.30, 0.20, 0.01, 0.60),
        Fundamentals("4444.T", "データ欠損の会社", 10_000_000_000, 0.30, 0.20, None, 0.60),
    ]
    assert upsert_fundamentals(conn, rows, today) == 4

    got = select_screened(conn, SCREEN, limit=50)
    assert [r["symbol"] for r in got] == ["1111.T"]


def test_upsert_is_idempotent_for_same_day(conn):
    today = date(2026, 9, 7)
    row = Fundamentals("1111.T", "会社", 10_000_000_000, 0.30, 0.20, 0.25, 0.60)
    upsert_fundamentals(conn, [row], today)
    updated = Fundamentals("1111.T", "会社（更新）", 10_000_000_000, 0.40, 0.20, 0.25, 0.60)
    upsert_fundamentals(conn, [updated], today)

    got = select_screened(conn, SCREEN, limit=50)
    assert len(got) == 1
    assert got[0]["name"] == "会社（更新）"
    assert got[0]["revenue_growth"] == pytest.approx(0.40)
