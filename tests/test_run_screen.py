from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from investment.jobs.run_screen import (
    SUCCESS_RATE_THRESHOLD,
    FetchResult,
    fetch_all,
    load_symbols,
    main,
    persist,
)
from investment.market import Fundamentals, MarketDataError


@pytest.fixture(autouse=True)
def no_sleep():
    """fetch_all() は連続アクセスを避けるため time.sleep(0.1) を挟むが、
    テストでは実際に待つ必要がないのでモックする。"""
    with patch("investment.jobs.run_screen.time.sleep"):
        yield


def _ok(code: str) -> Fundamentals:
    return Fundamentals(f"{code}.T", "会社", 1.0, 2.0, 3.0, 4.0, 5.0)


def test_load_symbols_skips_blank_and_comment_lines(tmp_path: Path):
    p = tmp_path / "symbols.txt"
    p.write_text("3993\n\n# コメント\n 7203 \n", encoding="utf-8")
    assert load_symbols(p) == ["3993", "7203"]


# --- 取得（fetch_all）: DBには一切触れない ---------------------------------


def test_fetch_all_never_touches_the_database():
    """本番で起きた障害の再現テスト。

    Neon は数分アクセスが無いと接続を切る。取得ループの途中でDBに書き込む
    設計だと、失敗と失敗の間隔が空いた時点で接続が死んでおり、次の書き込みで
    クラッシュする（本番では3,707件中1,600件目付近で発生した）。
    取得中はDBに一切触れないことを保証する。
    """

    def boom(*args, **kwargs):
        raise AssertionError("取得ループ中にDBへアクセスしてはいけない")

    def fake_fetch(code):
        if code == "9999":
            raise MarketDataError("取得できません")
        return _ok(code)

    with (
        patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch),
        patch("investment.jobs.run_screen.connect", side_effect=boom),
        patch("investment.jobs.run_screen.record_gaps", side_effect=boom),
        patch("investment.jobs.run_screen.upsert_fundamentals", side_effect=boom),
    ):
        result = fetch_all(["1111", "9999", "2222"])

    assert result.ok == 2
    assert result.failed == 1
    assert [f.symbol for f in result.fetched] == ["1111.T", "2222.T"]
    assert [scope for scope, _ in result.gaps] == ["fundamentals:9999"]


def test_main_opens_the_connection_only_after_all_fetching_is_done(tmp_path: Path):
    """接続を開くのは全銘柄の取得が終わったあと。

    先に接続を開いて取得ループを回すと、接続が待たされている間に
    Neon 側から切られる。呼び出し順そのものを検証する。
    """
    symbols_file = tmp_path / "symbols.txt"
    symbols_file.write_text("1111\n2222\n", encoding="utf-8")

    calls: list[str] = []

    def fake_fetch(code):
        calls.append(f"fetch:{code}")
        return _ok(code)

    class FakeConn:
        def __enter__(self):
            calls.append("connect")
            return self

        def __exit__(self, *exc):
            return False

    with (
        patch("investment.jobs.run_screen.SYMBOLS_JP", symbols_file),
        patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch),
        patch("investment.jobs.run_screen.connect", return_value=FakeConn()),
        patch("investment.jobs.run_screen.persist", return_value=True),
    ):
        assert main() == 0

    assert calls == ["fetch:1111", "fetch:2222", "connect"]


def test_fetch_all_treats_all_fields_none_as_failure_not_success():
    """C1: yfinance がレート制限で info={} を返すと、例外を投げずに全項目 None の
    Fundamentals が返ってくる。これを「取得成功」と数えてしまうと、成功率100%の
    まま空の行が保存され、前回の正常なデータを上書きしてしまう。"""

    def fake_fetch(code):
        if code == "5555":
            return Fundamentals("5555.T", None, None, None, None, None, None)
        return _ok(code)

    with patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch):
        result = fetch_all(["1111", "5555"])

    assert result.ok == 1
    assert result.failed == 1
    assert [f.symbol for f in result.fetched] == ["1111.T"]
    assert result.gaps[0][0] == "fundamentals:5555"


def test_fetch_all_treats_partial_none_fields_as_success():
    """一部の項目だけが None なのは正常な欠損。取得成功として扱う。"""
    partial = Fundamentals("1111.T", "会社", 1.0, None, 3.0, None, 5.0)

    with patch("investment.jobs.run_screen.fetch_fundamentals", return_value=partial):
        result = fetch_all(["1111"])

    assert result.ok == 1
    assert result.failed == 0
    assert result.gaps == []


# --- 保存（persist）: 成功率のゲート ---------------------------------------


def test_persist_writes_successes_and_records_gaps():
    """少数の失敗は許容し、成功分を保存しつつ失敗を data_gaps に残す。"""
    fetched = [_ok(str(1000 + i)) for i in range(19)]
    res = FetchResult(fetched=fetched, gaps=[("fundamentals:9999", "取得できません")])

    with (
        patch("investment.jobs.run_screen.upsert_fundamentals") as upsert,
        patch("investment.jobs.run_screen.record_gaps") as gaps,
    ):
        written = persist(conn=None, result=res, as_of=date(2026, 9, 7))

    assert written is True
    upsert.assert_called_once()
    assert upsert.call_args.args[1] == fetched
    gaps.assert_called_once()
    assert gaps.call_args.args[1] == [("fundamentals:9999", "取得できません")]


def test_persist_skips_write_and_records_gap_when_success_rate_below_threshold():
    """成功率が閾値(90%)未満のとき、書き込みを行わず、見送った旨を data_gaps に残す。

    通信不調でほとんど取得できなかった日に、少数の成功分だけで
    前回の正常なデータを上書きしてしまうことを防ぐための仕様。
    """
    res = FetchResult(
        fetched=[_ok("1111")],
        gaps=[("fundamentals:2222", "取得できません"), ("fundamentals:3333", "取得できません")],
    )  # 成功率 1/3 ≈ 33%

    with (
        patch("investment.jobs.run_screen.upsert_fundamentals") as upsert,
        patch("investment.jobs.run_screen.record_gaps") as gaps,
    ):
        written = persist(conn=None, result=res, as_of=date(2026, 9, 7))

    assert written is False
    upsert.assert_not_called()
    # 失敗2件 +「書き込みを見送った」1件 = 3件をまとめて1回で記録する
    recorded = gaps.call_args.args[1]
    assert len(recorded) == 3
    assert recorded[-1][0] == "fundamentals:batch"


def test_persist_writes_at_exactly_threshold_boundary():
    """成功率がちょうど閾値のときは書き込む（境界は「以上」）。"""
    assert SUCCESS_RATE_THRESHOLD == 0.9  # 閾値が黙って変わっていないこと
    total = 10
    ok = int(total * SUCCESS_RATE_THRESHOLD)  # 9
    res = FetchResult(
        fetched=[_ok(str(1000 + i)) for i in range(ok)],
        gaps=[("fundamentals:9999", "取得できません")],
    )

    with (
        patch("investment.jobs.run_screen.upsert_fundamentals") as upsert,
        patch("investment.jobs.run_screen.record_gaps"),
    ):
        written = persist(conn=None, result=res, as_of=date(2026, 9, 7))

    assert written is True
    upsert.assert_called_once()


def test_persist_records_nothing_extra_when_every_symbol_succeeded():
    """全件取得できた日は、data_gaps に余計な行を作らないこと。

    週次バッチが最もよく通る経路なので、明示的に押さえておく。
    """
    res = FetchResult(fetched=[_ok(str(1000 + i)) for i in range(10)], gaps=[])

    with (
        patch("investment.jobs.run_screen.upsert_fundamentals") as upsert,
        patch("investment.jobs.run_screen.record_gaps") as gaps,
    ):
        written = persist(conn=None, result=res, as_of=date(2026, 9, 7))

    assert written is True
    upsert.assert_called_once()
    gaps.assert_not_called()
