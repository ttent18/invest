import os

import pytest

from investment.config import SETTINGS
from investment.db import apply_migrations, connect, init_cash, select_cash

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


def test_init_cash_sets_initial_balance(conn):
    inserted = init_cash(conn, jpy=SETTINGS.initial_capital)

    assert inserted == 2
    cash = select_cash(conn)
    assert cash["JPY"] == SETTINGS.initial_capital
    assert cash["USD"] == 0


def test_init_cash_is_idempotent(conn):
    init_cash(conn, jpy=SETTINGS.initial_capital)
    inserted_second_run = init_cash(conn, jpy=SETTINGS.initial_capital)

    assert inserted_second_run == 0
    cash = select_cash(conn)
    # 2回実行しても残高が倍にならない
    assert cash["JPY"] == SETTINGS.initial_capital
    assert cash["USD"] == 0


def test_init_cash_does_not_overwrite_existing_balance(conn):
    # 取引などで既に残高が変わっている場合、上書きしてはならない
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES (%s, %s)", ("JPY", 400_000))
    conn.commit()

    init_cash(conn, jpy=SETTINGS.initial_capital)

    cash = select_cash(conn)
    assert cash["JPY"] == 400_000
