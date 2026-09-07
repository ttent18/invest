from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from investment.jobs.run_screen import SUCCESS_RATE_THRESHOLD, load_symbols, run
from investment.market import Fundamentals, MarketDataError


@pytest.fixture(autouse=True)
def no_sleep():
    """run() は連続アクセスを避けるため time.sleep(0.1) を挟むが、
    テストでは実際に待つ必要がないのでモックする。"""
    with patch("investment.jobs.run_screen.time.sleep"):
        yield


def test_load_symbols_skips_blank_and_comment_lines(tmp_path: Path):
    p = tmp_path / "symbols.txt"
    p.write_text("3993\n\n# コメント\n 7203 \n", encoding="utf-8")
    assert load_symbols(p) == ["3993", "7203"]


def test_run_saves_successes_and_counts_failures():
    """取得の成否を数え、成功分だけ保存する（少数の失敗は許容範囲）。

    ブリーフの元テストは3銘柄中1失敗（成功率67%）だったが、
    成功率90%未満は書き込みを見送る仕様に変更したため、
    このテストでは20銘柄中1失敗（成功率95%）に調整している。
    """
    saved: list[Fundamentals] = []

    def fake_upsert(conn, rows, as_of):
        saved.extend(rows)
        return len(rows)

    def fake_fetch(code):
        if code == "9999":
            raise MarketDataError("取得できません")
        return Fundamentals(f"{code}.T", "会社", 1.0, 2.0, 3.0, 4.0, 5.0)

    symbols = [f"{1000 + i}" for i in range(19)] + ["9999"]

    with (
        patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch),
        patch("investment.jobs.run_screen.upsert_fundamentals", side_effect=fake_upsert),
        patch("investment.jobs.run_screen.record_gap") as gap,
    ):
        ok, failed, written = run(symbols, conn=None, as_of=date(2026, 9, 7))

    assert ok == 19
    assert failed == 1
    assert written is True
    assert len(saved) == 19
    assert "9999.T" not in [f.symbol for f in saved]
    # 失敗した1件についてのみ data_gaps に記録する
    gap.assert_called_once()


def test_run_writes_when_success_rate_meets_threshold():
    """成功率が閾値(90%)以上のとき、書き込みが行われる。"""
    saved: list[Fundamentals] = []

    def fake_upsert(conn, rows, as_of):
        saved.extend(rows)
        return len(rows)

    def fake_fetch(code):
        return Fundamentals(f"{code}.T", "会社", 1.0, 2.0, 3.0, 4.0, 5.0)

    symbols = [f"{1000 + i}" for i in range(10)]  # 全件成功 = 成功率100%

    with (
        patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch),
        patch("investment.jobs.run_screen.upsert_fundamentals", side_effect=fake_upsert),
        patch("investment.jobs.run_screen.record_gap") as gap,
    ):
        ok, failed, written = run(symbols, conn=None, as_of=date(2026, 9, 7))

    assert ok == 10
    assert failed == 0
    assert written is True
    assert len(saved) == 10
    gap.assert_not_called()


def test_run_skips_write_and_records_gap_when_success_rate_below_threshold():
    """成功率が閾値(90%)未満のとき、書き込みを行わず、見送った旨を data_gaps に残す。

    通信不調でほとんど取得できなかった日に、少数の成功分だけで
    前回の正常なデータを上書きしてしまうことを防ぐための仕様。
    """

    def fake_fetch(code):
        if code in ("2222", "3333"):
            raise MarketDataError("取得できません")
        return Fundamentals(f"{code}.T", "会社", 1.0, 2.0, 3.0, 4.0, 5.0)

    symbols = ["1111", "2222", "3333"]  # 成功率 1/3 ≈ 33%

    with (
        patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch),
        patch("investment.jobs.run_screen.upsert_fundamentals") as upsert,
        patch("investment.jobs.run_screen.record_gap") as gap,
    ):
        ok, failed, written = run(symbols, conn=None, as_of=date(2026, 9, 7))

    assert ok == 1
    assert failed == 2
    assert written is False
    upsert.assert_not_called()
    # 失敗した2件 + 「書き込みを見送った」旨の1件 = 合計3回
    assert gap.call_count == 3


def test_run_writes_at_exactly_threshold_boundary():
    """成功率がちょうど90%（閾値以上）のとき、書き込みが行われる。"""
    assert SUCCESS_RATE_THRESHOLD == 0.9

    def fake_fetch(code):
        if code == "0010":
            raise MarketDataError("取得できません")
        return Fundamentals(f"{code}.T", "会社", 1.0, 2.0, 3.0, 4.0, 5.0)

    symbols = [f"{1 + i:04d}" for i in range(10)]  # 10銘柄中1失敗 = 成功率ちょうど90%

    with (
        patch("investment.jobs.run_screen.fetch_fundamentals", side_effect=fake_fetch),
        patch("investment.jobs.run_screen.upsert_fundamentals") as upsert,
        patch("investment.jobs.run_screen.record_gap") as gap,
    ):
        ok, failed, written = run(symbols, conn=None, as_of=date(2026, 9, 7))

    assert ok == 9
    assert failed == 1
    assert written is True
    upsert.assert_called_once()
    # 失敗した1件のみ記録。書き込みは行われたので見送りの記録は無い
    gap.assert_called_once()
