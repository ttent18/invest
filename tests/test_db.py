import os
from datetime import date

import psycopg
import pytest

from investment.config import SCREEN
from investment.db import (
    apply_migrations,
    connect,
    delete_push_subscription,
    mark_fill_failed,
    record_gap,
    record_gaps,
    save_last_prices,
    select_bucket_performance,
    select_capital,
    select_push_subscriptions,
    select_screened,
    select_unapplied_fills,
    upsert_fundamentals,
)
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
            cur.execute(
                "TRUNCATE fundamentals, trades, positions, proposals, cash, data_gaps, "
                "fills, push_subscriptions"
            )
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


def _gaps(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute("SELECT scope, detail FROM data_gaps ORDER BY id")
        return [(r["scope"], r["detail"]) for r in cur.fetchall()]


def test_record_gaps_writes_every_row_in_one_call(conn):
    """複数件の「取れなかった記録」を1回の書き込みで残せること。

    週次バッチは失敗を貯めておいて最後にまとめて書き出す。
    件数が多くても取りこぼさないことを実データベースで確認する。
    """
    rows = [(f"fundamentals:{1000 + i}", f"取得できません {i}") for i in range(5)]
    assert record_gaps(conn, rows) == 5
    assert _gaps(conn) == rows


def test_record_gaps_with_empty_list_writes_nothing(conn):
    """失敗が1件も無い日に、余計な行を作らないこと。"""
    assert record_gaps(conn, []) == 0
    assert _gaps(conn) == []


def test_record_gap_writes_a_single_row(conn):
    """1件だけ記録する既存の呼び出し方も従来どおり動くこと。"""
    record_gap(conn, scope="price:7203.T", detail="株価が取れません")
    assert _gaps(conn) == [("price:7203.T", "株価が取れません")]


def test_migrations_backfill_the_bucket_of_existing_proposals(conn):
    """v3 で追加した bucket 列が、既存の行にも埋まること。

    v1/v2 の提案は +22%/-8% で出したものなので「じっくり」に当たる。
    不明として NULL のまま残すと、枠ごとの集計から黙って漏れる。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO proposals
                (created_at, symbol, action, quantity, entry_price, take_profit,
                 stop_loss, required_win_rate, rationale, scenario, confidence,
                 strategy_tag, bucket, rule_version, journal_path)
            VALUES (NOW(), '1111.T', 'buy', 100, 1200, 1464, 1104, 0.2667,
                    'x', 'y', 'mid', 'z', 'じっくり', 'v3', 'journal/x.md')
            """
        )
        cur.execute("ALTER TABLE proposals ALTER COLUMN bucket DROP NOT NULL")
        cur.execute("UPDATE proposals SET bucket = NULL")
    conn.commit()

    apply_migrations(conn)  # 再適用で埋め直される

    with conn.cursor() as cur:
        cur.execute("SELECT bucket FROM proposals")
        assert [r["bucket"] for r in cur.fetchall()] == ["じっくり"]


def test_migrations_reject_an_unknown_bucket_name(conn):
    """知らない枠の名前は保存できないこと（打ち間違いを防ぐ）。"""
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO proposals
                (created_at, symbol, action, quantity, entry_price, take_profit,
                 stop_loss, required_win_rate, rationale, scenario, confidence,
                 strategy_tag, bucket, rule_version, journal_path)
            VALUES (NOW(), '1111.T', 'buy', 100, 1200, 1464, 1104, 0.2667,
                    'x', 'y', 'mid', 'z', 'なんとなく', 'v3', 'journal/x.md')
            """
        )


def test_every_bucket_defined_in_the_code_can_actually_be_saved(conn):
    """コードで定義した枠が、データベースにも保存できること。

    枠の名前はコード（config.BUCKETS）とマイグレーションの CHECK 制約の
    2箇所にある。片方だけ増やすと、検証は通るのに保存で落ちて、
    そのバッチの正当な提案まで道連れになる。
    """
    from investment.config import BUCKETS

    for i, b in enumerate(BUCKETS):
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO proposals
                    (created_at, symbol, action, quantity, entry_price, take_profit,
                     stop_loss, required_win_rate, rationale, scenario, confidence,
                     strategy_tag, bucket, rule_version, journal_path)
                VALUES (NOW(), %s, 'buy', 100, 1200, 1464, 1104, 0.2667,
                        'x', 'y', 'mid', 'z', %s, 'v3', 'journal/x.md')
                """,
                (f"{1000 + i}.T", b.name),
            )
            # positions 側も同じ制約を持つ。売りは保有から枠を読むので、
            # こちらだけ更新し忘れると売りが一切できなくなる。
            cur.execute(
                """
                INSERT INTO positions
                    (symbol, quantity, avg_price, currency, take_profit, stop_loss,
                     opened_at, bucket)
                VALUES (%s, 100, 1200, 'JPY', 1464, 1104, NOW(), %s)
                """,
                (f"{1000 + i}.T", b.name),
            )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT bucket FROM proposals ORDER BY id")
        assert [r["bucket"] for r in cur.fetchall()] == [b.name for b in BUCKETS]
        cur.execute("SELECT bucket FROM positions ORDER BY symbol")
        assert sorted(r["bucket"] for r in cur.fetchall()) == sorted(b.name for b in BUCKETS)


def test_select_capital_is_cash_plus_the_cost_of_what_we_hold(conn):
    """総資金 = 現金 + 保有の取得原価。

    現在の株価は使わない。含み益で次に買う金額が膨らむとリスクが勝手に増えるし、
    通信の失敗で総資金の計算が止まるのも筋が悪い。
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('JPY', 300000)")
        cur.execute(
            """
            INSERT INTO positions
                (symbol, quantity, avg_price, currency, take_profit, stop_loss,
                 opened_at, bucket)
            VALUES ('1111.T', 100, 1200, 'JPY', 1464, 1104, NOW(), 'じっくり')
            """
        )
    conn.commit()

    # 現金 300,000 + 100株 × 1,200円 = 420,000
    assert select_capital(conn) == 420000.0


def test_select_capital_with_no_positions_is_just_the_cash(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('JPY', 550000)")
    conn.commit()

    assert select_capital(conn) == 550000.0


def test_select_capital_is_zero_when_nothing_has_been_deposited(conn):
    """入金前でも例外を投げず 0 を返す。呼び出し側で「買えない」と判断できる。"""
    assert select_capital(conn) == 0.0


def test_select_capital_ignores_currencies_other_than_yen(conn):
    """いまは日本株だけを扱う。ドルを円に足すと桁が狂うので数えない。

    米国株を有効にするときに、為替を掛けて足す形へ直す。
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('JPY', 100000)")
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('USD', 5000)")
    conn.commit()

    assert select_capital(conn) == 100000.0


def test_fills_table_accepts_a_recorded_purchase(conn):
    """利用者が申告した約定を、そのまま1行入れられること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'buy', 100, 899, 'JPY')
            RETURNING id, applied_at, apply_error
            """
        )
        row = cur.fetchone()
    conn.commit()

    assert row["id"] > 0
    assert row["applied_at"] is None  # 入れた直後は未反映
    assert row["apply_error"] is None


def test_fills_table_rejects_a_side_that_is_neither_buy_nor_sell(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'なんとなく', 100, 899, 'JPY')
            """
        )


def test_fills_table_rejects_zero_or_negative_quantity(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'buy', 0, 899, 'JPY')
            """
        )


def test_trades_table_can_record_the_bucket_and_the_result(conn):
    """枠ごとの成績を出すために、売った時点の結果を取引に残せること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency,
                 bucket, realized_pnl, holding_days)
            VALUES (NOW(), '1111.T', 'sell', 100, 1100, 'JPY', '回転', 20000, 9)
            RETURNING bucket, realized_pnl, holding_days
            """
        )
        row = cur.fetchone()
    conn.commit()

    assert row["bucket"] == "回転"
    assert float(row["realized_pnl"]) == 20000.0
    assert row["holding_days"] == 9


def test_push_subscriptions_are_unique_per_endpoint(conn):
    """同じ端末から2回登録しても、行が増えないこと。"""
    sql = """
        INSERT INTO push_subscriptions (endpoint, p256dh, auth)
        VALUES ('https://example.test/abc', 'k1', 'a1')
        ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(sql)
        cur.execute("SELECT COUNT(*) AS c FROM push_subscriptions")
        assert cur.fetchone()["c"] == 1
    conn.commit()


def test_select_push_subscriptions_returns_the_registered_devices(conn):
    """登録した宛先が、そのままの内容で読み出せること。

    ここでモックを使ってしまうと、通知の宛先が実は取れていなくても
    テストが気づけない。実際のテーブルに対して SQL を走らせて確認する。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth)
            VALUES ('https://push.test/x', 'key-x', 'auth-x')
            """
        )
    conn.commit()

    rows = select_push_subscriptions(conn)

    assert rows == [
        {"endpoint": "https://push.test/x", "p256dh": "key-x", "auth": "auth-x"}
    ]


def test_select_push_subscriptions_with_none_registered_is_an_empty_list(conn):
    assert select_push_subscriptions(conn) == []


def test_delete_push_subscription_removes_only_the_given_endpoint(conn):
    """指定した宛先だけが消え、他の宛先は残ること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth)
            VALUES ('https://push.test/keep', 'k1', 'a1'),
                   ('https://push.test/remove', 'k2', 'a2')
            """
        )
    conn.commit()

    delete_push_subscription(conn, "https://push.test/remove")

    remaining = select_push_subscriptions(conn)
    assert [r["endpoint"] for r in remaining] == ["https://push.test/keep"]


def test_delete_push_subscription_does_nothing_for_an_unknown_endpoint(conn):
    """存在しない宛先を指定しても例外にならず、既存の行はそのまま残ること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO push_subscriptions (endpoint, p256dh, auth)
            VALUES ('https://push.test/keep', 'k1', 'a1')
            """
        )
    conn.commit()

    delete_push_subscription(conn, "https://push.test/does-not-exist")

    remaining = select_push_subscriptions(conn)
    assert [r["endpoint"] for r in remaining] == ["https://push.test/keep"]


def test_select_unapplied_fills_returns_only_the_ones_not_yet_applied(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency, applied_at)
            VALUES ('1111.T', 'buy', 100, 900, 'JPY', NOW()),
                   ('2222.T', 'buy', 100, 800, 'JPY', NULL),
                   ('3333.T', 'sell', 100, 700, 'JPY', NULL)
            """
        )
    conn.commit()

    rows = select_unapplied_fills(conn)

    assert [r["symbol"] for r in rows] == ["2222.T", "3333.T"]   # 古い順


def test_mark_fill_failed_keeps_the_row_and_records_the_reason(conn):
    """反映できなかった記録を消さないこと。理由を残して利用者に見せる。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'sell', 100, 900, 'JPY') RETURNING id
            """
        )
        fill_id = cur.fetchone()["id"]
    conn.commit()

    mark_fill_failed(conn, fill_id, "1111.T を保有していません")

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fill_id,))
        row = cur.fetchone()
    assert row["applied_at"] is None                      # 未反映のまま
    assert "保有していません" in row["apply_error"]


def _sell_trade(conn, bucket: str, pnl: float, days: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency,
                 bucket, realized_pnl, holding_days)
            VALUES (NOW(), '1111.T', 'sell', 100, 1000, 'JPY', %s, %s, %s)
            """,
            (bucket, pnl, days),
        )
    conn.commit()


def test_bucket_performance_counts_only_closed_trades(conn):
    """成績は「売って決着した取引」だけで数える。買っただけの分は含めない。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency, bucket)
            VALUES (NOW(), '1111.T', 'buy', 100, 900, 'JPY', 'じっくり')
            """
        )
    conn.commit()
    _sell_trade(conn, "じっくり", pnl=20000, days=30)

    rows = {r["bucket"]: r for r in select_bucket_performance(conn)}
    assert rows["じっくり"]["closed"] == 1


def test_bucket_performance_reports_the_win_rate_and_the_total(conn):
    _sell_trade(conn, "じっくり", pnl=20000, days=30)
    _sell_trade(conn, "じっくり", pnl=-7000, days=12)
    _sell_trade(conn, "じっくり", pnl=15000, days=40)
    _sell_trade(conn, "回転", pnl=-3000, days=8)

    rows = {r["bucket"]: r for r in select_bucket_performance(conn)}

    patient = rows["じっくり"]
    assert patient["closed"] == 3
    assert patient["wins"] == 2
    assert patient["win_rate"] == pytest.approx(2 / 3)
    assert patient["total_pnl"] == pytest.approx(28000.0)
    assert patient["avg_holding_days"] == pytest.approx((30 + 12 + 40) / 3)

    fast = rows["回転"]
    assert fast["closed"] == 1
    assert fast["wins"] == 0
    assert fast["win_rate"] == pytest.approx(0.0)


def test_bucket_performance_includes_the_breakeven_win_rate_to_compare_against(conn):
    """勝率だけでは意味がない。損益トントンの勝率と並べて初めて判断できる。

    じっくり枠は 0.08 / (0.22 + 0.08) = 26.7%、
    回転枠は 0.05 / (0.10 + 0.05) = 33.3%。
    """
    rows = {r["bucket"]: r for r in select_bucket_performance(conn)}

    assert rows["じっくり"]["breakeven_win_rate"] == pytest.approx(0.2667, abs=0.001)
    assert rows["回転"]["breakeven_win_rate"] == pytest.approx(0.3333, abs=0.001)


def test_bucket_performance_lists_every_bucket_even_with_no_trades(conn):
    """まだ1件も決着していない枠も、0件として出す。

    行が消えると「まだ始まっていない」のか「集計から漏れた」のか分からない。
    """
    rows = select_bucket_performance(conn)

    assert [r["bucket"] for r in rows] == ["じっくり", "回転"]
    assert all(r["closed"] == 0 for r in rows)
    assert all(r["win_rate"] is None for r in rows)          # 0件なら勝率は「無い」
    assert all(r["avg_holding_days"] is None for r in rows)


def test_save_last_prices_updates_only_the_symbols_given(conn):
    """取れた銘柄だけ更新し、取れなかった銘柄の古い値を消さないこと。

    古い値が残っていれば「いつ時点の値か」を添えて表示できる。
    消してしまうと、画面に何も出せなくなる。
    """
    with conn.cursor() as cur:
        for sym in ("1111.T", "2222.T"):
            cur.execute(
                """
                INSERT INTO positions
                    (symbol, quantity, avg_price, currency, take_profit, stop_loss,
                     opened_at, bucket, last_price, last_price_at)
                VALUES (%s, 100, 900, 'JPY', 1098, 828, NOW(), 'じっくり', 850, NOW())
                """,
                (sym,),
            )
    conn.commit()

    assert save_last_prices(conn, {"1111.T": 910.0}) == 1

    with conn.cursor() as cur:
        cur.execute("SELECT symbol, last_price FROM positions ORDER BY symbol")
        rows = {r["symbol"]: float(r["last_price"]) for r in cur.fetchall()}
    assert rows["1111.T"] == 910.0     # 更新された
    assert rows["2222.T"] == 850.0     # 古い値が残っている


def test_save_last_prices_with_nothing_to_save_writes_nothing(conn):
    assert save_last_prices(conn, {}) == 0
