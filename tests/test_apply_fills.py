"""申告を反映するジョブのテスト。実際のデータベースを使う。"""

import os
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import investment.db
from investment.config import bucket_by_name
from investment.db import apply_migrations, connect, init_cash, save_fill_result
from investment.fills import apply_buy
from investment.jobs.apply_fills import main, run

pytestmark = pytest.mark.integration

TEST_URL = os.environ.get("DATABASE_URL_TEST")
JST = ZoneInfo("Asia/Tokyo")


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


def _proposal(conn, symbol: str, bucket: str, action: str = "buy") -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO proposals
                (created_at, symbol, action, quantity, entry_price, take_profit,
                 stop_loss, required_win_rate, rationale, scenario, confidence,
                 strategy_tag, bucket, rule_version, journal_path)
            VALUES (NOW(), %s, %s, 100, 900, 1098, 828, 0.2667,
                    'x', 'y', 'mid', 'z', %s, 'v3', 'journal/x.md')
            RETURNING id
            """,
            (symbol, action, bucket),
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


def test_applying_a_sell_marks_the_proposal_as_taken(conn):
    """提案どおりに売ったら、その提案も「実行した」にすること。

    以前の実装は `proposal_id = row["proposal_id"] if fill["side"] == "buy"
    else None` としており、売りのときは提案の紐付けをジョブ側で
    捨てていた。すると売った後も提案が pending のまま画面に残り、
    「まだ実行していない提案」として出続ける（一部売却なら、
    利用者がそれを見てもう一度売る操作をし、二重に売ってしまう
    事故にもつながる）。
    """
    pid_buy = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid_buy, symbol="1111.T", side="buy", quantity=100, price=900)
    run(conn, today=date(2026, 9, 8))

    pid_sell = _proposal(conn, "1111.T", "じっくり", action="sell")
    _fill(
        conn, proposal_id=pid_sell, symbol="1111.T", side="sell", quantity=100, price=1100
    )
    run(conn, today=date(2026, 9, 30))

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid_sell,))
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

        with conn.cursor() as cur:
            cur.execute("SELECT recorded_at FROM fills WHERE id = %s", (fid,))
            recorded_at = cur.fetchone()["recorded_at"]

        with pytest.raises(Exception):  # noqa: B017 - トリガーが投げる例外を確認したいだけ
            save_fill_result(
                conn, fid, result.position, result.cash_delta, result.trade,
                "JPY", recorded_at, proposal_id=pid,
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


def test_buying_more_of_the_same_position_does_not_change_when_it_was_first_opened(conn):
    """買い増しても「最初に持った日」（opened_at）が動かないこと。

    opened_at は回転枠の期限（10営業日）の起点。買い増すたびに動くと、
    期限という仕組みが骨抜きになる。`save_fill_result` の
    `ON CONFLICT ... DO UPDATE` は opened_at を更新対象に入れていないが、
    それを確かめるテストがこれまで無かった（将来
    `opened_at = EXCLUDED.opened_at` を足しても、他のテストは
    通ったままになってしまう）。
    """
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    run(conn, today=date(2026, 9, 8))

    # 「最初に持った日」を、はっきり見分けられる値に固定してから買い増す。
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE positions SET opened_at = '2020-01-01T00:00:00+00' "
            "WHERE symbol = '1111.T' RETURNING opened_at"
        )
        first_opened_at = cur.fetchone()["opened_at"]
    conn.commit()

    pid2 = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid2, symbol="1111.T", side="buy", quantity=50, price=950)
    assert run(conn, today=date(2026, 9, 9)) == (1, 0)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT opened_at, quantity FROM positions WHERE symbol = '1111.T'"
        )
        row = cur.fetchone()

    assert row["opened_at"] == first_opened_at   # 買い増しても動いていないこと
    assert row["quantity"] == 150


# --- 保有日数・取引の日時は申告した時刻を使う（指摘4） -----------------------
# NOW()（ジョブが実行された時刻）ではなく fills.recorded_at（利用者が申告した
# 時刻）を使う。月曜の場中に買っても NOW() を使うとジョブが翌朝に走るぶん
# opened_at が翌朝になり、保有日数がずれる。回転枠の期限判定や枠ごとの成績
# （この計画の目的そのもの）を歪める。


def test_buy_uses_the_recorded_time_not_the_job_run_time(conn):
    pid = _proposal(conn, "1111.T", "じっくり")
    fid = _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    with conn.cursor() as cur:
        # 申告した時刻を、ジョブの実行日(2026-09-08)とはっきり違う日にする
        cur.execute(
            "UPDATE fills SET recorded_at = '2026-09-01T10:00:00+09' WHERE id = %s",
            (fid,),
        )
    conn.commit()

    run(conn, today=date(2026, 9, 8))

    with conn.cursor() as cur:
        cur.execute("SELECT opened_at FROM positions WHERE symbol = '1111.T'")
        opened_at = cur.fetchone()["opened_at"]
        cur.execute(
            "SELECT executed_at FROM trades WHERE symbol = '1111.T' AND side = 'buy'"
        )
        executed_at = cur.fetchone()["executed_at"]

    expected = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    assert opened_at == expected
    assert executed_at == expected


def test_sell_uses_the_recorded_time_for_the_trade_but_not_for_holding_days(conn):
    """売りの取引日時も申告時刻を使うこと（holding_days自体は計画2-Bへ持ち越し）。"""
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    run(conn, today=date(2026, 9, 1))

    pid_sell = _proposal(conn, "1111.T", "じっくり", action="sell")
    fid_sell = _fill(
        conn, proposal_id=pid_sell, symbol="1111.T", side="sell", quantity=100, price=1100
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fills SET recorded_at = '2026-09-10T15:00:00+09' WHERE id = %s",
            (fid_sell,),
        )
    conn.commit()

    run(conn, today=date(2026, 9, 30))

    with conn.cursor() as cur:
        cur.execute(
            "SELECT executed_at FROM trades WHERE symbol = '1111.T' AND side = 'sell'"
        )
        executed_at = cur.fetchone()["executed_at"]

    expected = datetime(2026, 9, 10, 15, 0, 0, tzinfo=timezone(timedelta(hours=9)))
    assert executed_at == expected


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


# --- 申告と提案の突き合わせ（指摘3） -----------------------------------------
# fills.symbol と proposals.symbol が一致するか、その提案が既に taken か、
# の2つを確かめずに反映すると、二重売買の入口になる。


def test_a_fill_whose_symbol_does_not_match_the_linked_proposal_is_rejected(conn):
    """画面で違うカードを押した場合を想定する。

    A社の買いをB社の提案に紐づけて記録すると、そのまま反映すればB社の
    提案が「実行した」ことになり、A社の提案は pending のまま残って
    もう一度買う提案として出てしまう。反映そのものを止める必要がある。
    """
    pid = _proposal(conn, "2222.T", "じっくり")  # B社の提案
    fid = _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)  # A社の申告

    assert run(conn, today=date(2026, 9, 8)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fid,))
        row = cur.fetchone()
    assert row["applied_at"] is None
    assert "一致しません" in row["apply_error"]

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid,))
        assert cur.fetchone()["outcome"] == "pending"  # B社の提案は taken になっていない

    assert _state(conn)["positions"] == []  # A社の保有も作られていない


def test_a_second_fill_for_an_already_taken_proposal_is_rejected(conn):
    """同じ提案に対する申告が2件あったら、2件目は反映しないこと。

    1件目の申告で提案は taken になる。2件目をそのまま反映すると、
    同じ買いが二重に保有・現金へ反映されてしまう。
    """
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    assert run(conn, today=date(2026, 9, 8)) == (1, 0)

    fid2 = _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    assert run(conn, today=date(2026, 9, 9)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fid2,))
        row = cur.fetchone()
    assert row["applied_at"] is None
    assert "既に実行済み" in row["apply_error"]

    s = _state(conn)
    assert len(s["positions"]) == 1
    assert s["positions"][0]["quantity"] == 100  # 二重に反映されていない


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

    **売りで確かめる。** 買いだと `run()` が先に `select_cash(conn).get(...)`
    で現金を読み（行が無ければ 0.0）、`apply_buy` の「現金が足りません」
    チェックにそこで弾かれてしまい、`db.save_fill_result` の中にある
    `cur.rowcount == 0` の守り（現金の行が無いこと自体を検出する処理）
    には一度も到達しない。それでは守りを丸ごと消してもテストが
    緑のまま通ってしまい、確認になっていない。売りは現金を先読み
    しないので、必ずこの守りに到達する（レビューで確認済み）。

    取引の追加や保有の更新だけ進んで現金が動かない状態は、帳尻が
    合わなくなる一番避けたい壊れ方。反映は取り消し、fills には
    理由を残す。
    """
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    run(conn, today=date(2026, 9, 8))
    before = _state(conn)

    with conn.cursor() as cur:
        cur.execute("DELETE FROM cash WHERE currency = 'JPY'")
    conn.commit()

    pid_sell = _proposal(conn, "1111.T", "じっくり", action="sell")
    fid = _fill(
        conn, proposal_id=pid_sell, symbol="1111.T", side="sell", quantity=100, price=1100
    )

    assert run(conn, today=date(2026, 9, 30)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fid,))
        row = cur.fetchone()
    assert row["applied_at"] is None
    assert "現金" in row["apply_error"]

    s = _state(conn)
    assert s["positions"] == before["positions"]   # 保有が減っていないこと（全部取り消し）
    assert len(s["trades"]) == len(before["trades"])   # 売りの取引が増えていないこと

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid_sell,))
        # 提案も pending のまま残ること。反映は同じトランザクションで
        # 提案を taken にするところまで含むので、現金が原因で全体が
        # 取り消されるなら提案も一緒に取り消される。ここで taken に
        # なっていたら、反映されていないのに実行済み扱いになる欠陥。
        assert cur.fetchone()["outcome"] == "pending"


def test_a_database_error_on_one_fill_does_not_stop_the_others(conn):
    """1件の申告でデータベース側のエラーが起きても、他の申告は反映されること。

    これまで run() は FillError しか捕まえていなかった。計画2-B で画面から
    入る申告の種類が増えるため、想定外の失敗で残り全部が道連れになる形を
    先に塞ぐ。notify.send と同じ理由。
    """
    from unittest.mock import patch

    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    pid2 = _proposal(conn, "2222.T", "じっくり")
    _fill(conn, proposal_id=pid2, symbol="2222.T", side="buy", quantity=100, price=800)

    calls = {"n": 0}
    real = investment.db.save_fill_result

    def fail_first(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("データベース側の想定外のエラー")
        return real(*args, **kwargs)

    with patch("investment.jobs.apply_fills.save_fill_result", side_effect=fail_first):
        ok, failed = run(conn, today=date(2026, 9, 8))

    assert (ok, failed) == (1, 1)
    with conn.cursor() as cur:
        cur.execute("SELECT symbol FROM positions")
        assert [r["symbol"] for r in cur.fetchall()] == ["2222.T"]
        cur.execute("SELECT apply_error FROM fills WHERE symbol = '1111.T'")
        assert "想定外" in cur.fetchone()["apply_error"]


def test_main_notifies_when_some_fills_could_not_be_applied(tmp_path):
    """反映できなかった記録があることを、利用者に届けること。

    これまでは fills.apply_error に溜まるだけで、ジョブは成功扱い・通知なしだった。
    「データを黙って落とさない」は満たしていたが、「気づける」は満たしていなかった。
    """
    from unittest.mock import patch

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.apply_fills.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.apply_fills.run", return_value=(0, 2)),
        patch("investment.jobs.apply_fills.select_capital", return_value=550_000.0),
        patch("investment.jobs.apply_fills.notify_send") as notify,
    ):
        assert main() == 0

    notify.assert_called_once()
    assert "2" in notify.call_args.kwargs["title"] or "2" in notify.call_args.kwargs["body"]


def test_main_does_not_notify_when_everything_was_applied(tmp_path):
    from unittest.mock import patch

    class FakeConn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.apply_fills.connect", side_effect=lambda: FakeConn()),
        patch("investment.jobs.apply_fills.run", return_value=(2, 0)),
        patch("investment.jobs.apply_fills.select_capital", return_value=550_000.0),
        patch("investment.jobs.apply_fills.notify_send") as notify,
    ):
        assert main() == 0

    notify.assert_not_called()


# --- 売買日時（traded_at） --------------------------------------------------
# recorded_at（画面で入力した時刻）を使うと、翌日に入力したときに1日ずれる。
# 利用者が実際に売買した日時（traded_at）があれば、そちらを使う。


def test_the_traded_time_is_used_when_the_user_gave_one(conn):
    """利用者が入力した売買日時があれば、それを使うこと。

    入力した時刻（recorded_at）ではなく、実際に売買した時刻を使う。
    翌日に入力すると1日ずれるため。保有日数の正確さは、どちらの枠が
    向いているかを測るというこの仕組みの目的に直結する。
    """
    pid = _proposal(conn, "1111.T", "じっくり")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency,
                               traded_at)
            VALUES (%s, '1111.T', 'buy', 100, 900, 'JPY',
                    TIMESTAMPTZ '2026-09-01 10:30:00+09')
            """,
            (pid,),
        )
    conn.commit()

    run(conn, today=date(2026, 9, 8))

    with conn.cursor() as cur:
        cur.execute("SELECT opened_at FROM positions WHERE symbol = '1111.T'")
        opened = cur.fetchone()["opened_at"]
        cur.execute("SELECT executed_at FROM trades WHERE symbol = '1111.T'")
        executed = cur.fetchone()["executed_at"]

    assert opened.astimezone(JST).date() == date(2026, 9, 1)
    assert executed.astimezone(JST).date() == date(2026, 9, 1)


def test_the_recorded_time_is_used_when_no_traded_time_was_given(conn):
    """売買日時が無ければ、これまでどおり申告した時刻を使うこと。"""
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    run(conn, today=date(2026, 9, 8))

    with conn.cursor() as cur:
        cur.execute("SELECT opened_at FROM positions WHERE symbol = '1111.T'")
        assert cur.fetchone()["opened_at"] is not None
