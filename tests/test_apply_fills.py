"""申告を反映するジョブのテスト。実際のデータベースを使う。"""

import os
from datetime import date

import pytest

from investment.config import bucket_by_name
from investment.db import apply_migrations, connect, init_cash, save_fill_result
from investment.fills import apply_buy
from investment.jobs.apply_fills import run

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
        init_cash(c, jpy=550_000)
        yield c


def _proposal(conn, symbol: str, bucket: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO proposals
                (created_at, symbol, action, quantity, entry_price, take_profit,
                 stop_loss, required_win_rate, rationale, scenario, confidence,
                 strategy_tag, bucket, rule_version, journal_path)
            VALUES (NOW(), %s, 'buy', 100, 900, 1098, 828, 0.2667,
                    'x', 'y', 'mid', 'z', %s, 'v3', 'journal/x.md')
            RETURNING id
            """,
            (symbol, bucket),
        )
        pid = cur.fetchone()["id"]
    conn.commit()
    return pid


def _fill(conn, **kw) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency, fee)
            VALUES (%(proposal_id)s, %(symbol)s, %(side)s, %(quantity)s,
                    %(price)s, %(currency)s, %(fee)s)
            RETURNING id
            """,
            {"proposal_id": None, "currency": "JPY", "fee": 0, **kw},
        )
        fid = cur.fetchone()["id"]
    conn.commit()
    return fid


def _state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM positions ORDER BY symbol")
        positions = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT amount FROM cash WHERE currency = 'JPY'")
        cash_row = cur.fetchone()
        cash = float(cash_row["amount"]) if cash_row else None
        cur.execute("SELECT * FROM trades ORDER BY id")
        trades = [dict(r) for r in cur.fetchall()]
    return {"positions": positions, "cash": cash, "trades": trades}


def test_applying_a_buy_creates_the_position_and_reduces_the_cash(conn):
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (1, 0)

    s = _state(conn)
    assert len(s["positions"]) == 1
    assert s["positions"][0]["symbol"] == "1111.T"
    assert s["positions"][0]["quantity"] == 100
    assert s["positions"][0]["bucket"] == "じっくり"
    assert s["cash"] == 550_000 - 90_000
    assert len(s["trades"]) == 1
    assert s["trades"][0]["bucket"] == "じっくり"


def test_applying_a_buy_marks_the_proposal_as_taken(conn):
    """提案どおりに買ったら、その提案を「実行した」にすること。

    pending のまま残ると、次の実行でも「まだ買っていない提案」として出てくる。
    """
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    run(conn, today=date(2026, 9, 8))

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid,))
        assert cur.fetchone()["outcome"] == "taken"


def test_a_failed_proposal_update_leaves_no_trade_and_no_position_either(conn):
    """提案を taken にする更新が失敗したら、取引も保有も残さないこと。

    以前の作りは、反映（取引・保有・現金・fills）と「提案を taken に
    する」更新が別々のトランザクションだった。反映が終わった直後に
    プロセスが落ちると、反映は完了しているのに提案だけ pending の
    まま残り、スマホの画面に同じ提案が「まだ買っていない提案」として
    また出てきて、二重に買う事故につながっていた。

    ここでは提案の更新だけをデータベースのトリガーでわざと失敗させ、
    反映（取引・保有・現金）が提案の更新と運命を共にする ―
    つまり同じトランザクションに入っている ― ことを、実際の
    データベースの状態で確かめる。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE FUNCTION test_block_proposal_taken() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'test: 提案の更新をわざと失敗させる';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        cur.execute(
            """
            CREATE TRIGGER test_block_proposal_taken
            BEFORE UPDATE ON proposals
            FOR EACH ROW EXECUTE FUNCTION test_block_proposal_taken()
            """
        )
    conn.commit()

    try:
        pid = _proposal(conn, "1111.T", "じっくり")
        fid = _fill(
            conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900
        )

        rule = bucket_by_name("じっくり")
        result = apply_buy(
            None,
            {
                "symbol": "1111.T", "side": "buy", "quantity": 100, "price": 900,
                "currency": "JPY", "fee": 0,
            },
            rule,
            550_000,
        )

        with pytest.raises(Exception):  # noqa: B017 - トリガーが投げる例外を確認したいだけ
            save_fill_result(
                conn, fid, result.position, result.cash_delta, result.trade,
                "JPY", proposal_id=pid,
            )
        conn.rollback()  # ジョブ本体（run）が失敗時に必ず行うのと同じ後始末

        s = _state(conn)
        assert s["positions"] == []   # 保有が作られたままになっていないこと
        assert s["trades"] == []      # 取引が残ったままになっていないこと
        assert s["cash"] == 550_000   # 現金も動いていないこと

        with conn.cursor() as cur:
            cur.execute("SELECT applied_at FROM fills WHERE id = %s", (fid,))
            assert cur.fetchone()["applied_at"] is None   # 未反映のまま

            cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid,))
            assert cur.fetchone()["outcome"] == "pending"
    finally:
        with conn.cursor() as cur:
            cur.execute("DROP TRIGGER IF EXISTS test_block_proposal_taken ON proposals")
            cur.execute("DROP FUNCTION IF EXISTS test_block_proposal_taken()")
        conn.commit()


def test_applying_a_sell_removes_the_position_and_adds_the_cash(conn):
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    run(conn, today=date(2026, 9, 8))

    _fill(conn, symbol="1111.T", side="sell", quantity=100, price=1100)
    assert run(conn, today=date(2026, 9, 30)) == (1, 0)

    s = _state(conn)
    assert s["positions"] == []
    assert s["cash"] == 550_000 - 90_000 + 110_000
    sell = s["trades"][-1]
    assert sell["side"] == "sell"
    assert float(sell["realized_pnl"]) == 20_000.0
    assert sell["bucket"] == "じっくり"


def test_a_fill_that_cannot_be_applied_is_kept_with_its_reason(conn):
    """反映できない申告を黙って消さないこと。"""
    fid = _fill(conn, symbol="9999.T", side="sell", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fid,))
        row = cur.fetchone()
    assert row["applied_at"] is None
    assert "保有していません" in row["apply_error"]


def test_one_bad_fill_does_not_stop_the_others(conn):
    """1件の失敗で全体を止めないこと。"""
    _fill(conn, symbol="9999.T", side="sell", quantity=100, price=900)   # 失敗する
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (1, 1)
    assert len(_state(conn)["positions"]) == 1


def test_a_buy_without_a_proposal_is_rejected(conn):
    """買いは提案に紐づいていないと、どの枠か決められない。"""
    fid = _fill(conn, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT apply_error FROM fills WHERE id = %s", (fid,))
        assert "枠" in cur.fetchone()["apply_error"]


def test_applying_the_same_fill_twice_does_not_double_count(conn):
    """反映済みの申告を二度反映しないこと。"""
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    run(conn, today=date(2026, 9, 8))
    assert run(conn, today=date(2026, 9, 8)) == (0, 0)   # 2回目は対象なし

    s = _state(conn)
    assert s["positions"][0]["quantity"] == 100
    assert len(s["trades"]) == 1


def test_a_missing_cash_row_leaves_the_fill_unapplied_with_a_reason(conn):
    """現金の行が無い通貨は、黙って現金だけ動かないまま成功にしないこと。

    取引の追加や保有の作成だけ進んで現金が動かない状態は、
    帳尻が合わなくなる一番避けたい壊れ方。反映は取り消し、
    fills には理由を残す。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM cash WHERE currency = 'JPY'")
    conn.commit()

    pid = _proposal(conn, "1111.T", "じっくり")
    fid = _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fid,))
        row = cur.fetchone()
    assert row["applied_at"] is None
    assert row["apply_error"]

    s = _state(conn)
    assert s["positions"] == []   # 保有も作られていないこと（全部取り消し）
    assert s["trades"] == []      # 取引も残っていないこと（全部取り消し）

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid,))
        # 提案も pending のまま残ること。反映は同じトランザクションで
        # 提案を taken にするところまで含むので、現金が原因で全体が
        # 取り消されるなら提案も一緒に取り消される。ここで taken に
        # なっていたら、反映されていないのに実行済み扱いになる欠陥。
        assert cur.fetchone()["outcome"] == "pending"
