from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from investment.jobs.morning_check import (
    LOOKBACK_DAYS,
    _lookback_window,
    detect_hits,
    run,
    summarize_result,
)
from investment.market import MarketDataError

POSITIONS = [
    {"symbol": "3993.T", "take_profit": 2989.0, "stop_loss": 2254.0},
    {"symbol": "AAPL", "take_profit": 220.0, "stop_loss": 168.0},
]


def test_detects_stop_loss_hit():
    ranges = {"3993.T": (2500.0, 2200.0), "AAPL": (200.0, 190.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert len(hits) == 1
    assert hits[0]["symbol"] == "3993.T"
    assert hits[0]["kind"] == "stop_loss"
    assert hits[0]["estimated_price"] == 2254.0


def test_detects_take_profit_hit():
    ranges = {"3993.T": (2500.0, 2400.0), "AAPL": (225.0, 210.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert len(hits) == 1
    assert hits[0]["symbol"] == "AAPL"
    assert hits[0]["kind"] == "take_profit"


def test_boundary_exactly_at_stop_loss_counts_as_hit():
    ranges = {"3993.T": (2500.0, 2254.0), "AAPL": (200.0, 190.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert [h["symbol"] for h in hits] == ["3993.T"]


def test_boundary_exactly_at_take_profit_counts_as_hit():
    ranges = {"3993.T": (2500.0, 2400.0), "AAPL": (220.0, 210.0)}
    hits = detect_hits(POSITIONS, ranges)
    assert [h["symbol"] for h in hits] == ["AAPL"]


def test_no_hits_returns_empty():
    ranges = {"3993.T": (2500.0, 2400.0), "AAPL": (200.0, 190.0)}
    assert detect_hits(POSITIONS, ranges) == []


def test_missing_range_is_skipped_not_treated_as_no_hit():
    hits = detect_hits(POSITIONS, {"AAPL": (225.0, 210.0)})
    assert [h["symbol"] for h in hits] == ["AAPL"]


# ---------------------------------------------------------------------------
# 修正1: 「前日」ではなく直近の一定期間(LOOKBACK_DAYS)を見る。
# opened_at より前は見ない（買う前の値動きを誤検知しない）。
# ---------------------------------------------------------------------------


def test_lookback_days_is_five():
    # 3連休（土日+祝日、または祝日+土日）をまたいでも直近の取引日を拾える幅。
    assert LOOKBACK_DAYS == 5


def test_lookback_window_starts_at_lookback_boundary_when_opened_long_ago():
    today = date(2026, 9, 7)  # 月曜
    opened = date(2026, 1, 1)  # ずっと前に開いたポジション
    start, end = _lookback_window(opened, today)
    assert start == today - timedelta(days=LOOKBACK_DAYS)
    assert end == today - timedelta(days=1)


def test_lookback_window_starts_at_opened_at_when_opened_recently():
    # opened_at より前は見ない
    today = date(2026, 9, 7)
    opened = date(2026, 9, 6)  # 前日に開いた
    start, end = _lookback_window(opened, today)
    assert start == opened
    assert end == today - timedelta(days=1)


def test_lookback_window_is_none_when_opened_today():
    # 今日開いたばかりのポジションは、まだ確認すべき期間がない
    today = date(2026, 9, 7)
    assert _lookback_window(today, today) is None


def test_lookback_window_boundary_exactly_at_lookback_days():
    today = date(2026, 9, 7)
    opened = today - timedelta(days=LOOKBACK_DAYS)
    start, end = _lookback_window(opened, today)
    assert start == opened
    assert end == today - timedelta(days=1)


TODAY = date(2026, 9, 7)

RUN_POSITIONS = [
    {
        "symbol": "3993.T",
        "take_profit": 2989.0,
        "stop_loss": 2254.0,
        "opened_at": datetime(2026, 9, 1, tzinfo=UTC),
    },
    {
        "symbol": "AAPL",
        "take_profit": 220.0,
        "stop_loss": 168.0,
        "opened_at": datetime(2026, 9, 1, tzinfo=UTC),
    },
]


def test_run_does_not_query_before_opened_at():
    # opened_at が LOOKBACK_DAYS より最近であれば、開始日は opened_at になる
    # （買う前の値動きを問い合わせない = 誤検知の元を断つ）。
    opened = TODAY - timedelta(days=2)
    positions = [
        {"symbol": "3993.T", "take_profit": 2989.0, "stop_loss": 2254.0, "opened_at": opened},
    ]
    with patch(
        "investment.jobs.morning_check.fetch_range", return_value=(2500.0, 2400.0)
    ) as fr:
        run(positions, conn=None, today=TODAY)

    fr.assert_called_once_with("3993.T", opened, TODAY - timedelta(days=1))


def test_run_detects_hit_found_anywhere_in_window():
    # 期間内のどこかで損切り価格に触れていれば検知する
    def fake_fetch(symbol, start, end):
        if symbol == "3993.T":
            return (2500.0, 2200.0)  # 安値が損切りを下回った
        return (200.0, 190.0)

    with patch("investment.jobs.morning_check.fetch_range", side_effect=fake_fetch):
        hits, failed, attempted = run(RUN_POSITIONS, conn=None, today=TODAY)

    assert [h["symbol"] for h in hits] == ["3993.T"]
    assert failed == 0
    assert attempted == 2


def test_run_counts_failures_and_records_gap():
    def fake_fetch(symbol, start, end):
        if symbol == "3993.T":
            raise MarketDataError("取れません")
        return (200.0, 190.0)

    with (
        patch("investment.jobs.morning_check.fetch_range", side_effect=fake_fetch),
        patch("investment.jobs.morning_check.record_gap") as gap,
    ):
        hits, failed, attempted = run(RUN_POSITIONS, conn=None, today=TODAY)

    assert hits == []
    assert failed == 1
    assert attempted == 2
    gap.assert_called_once()


def test_run_skips_fetch_and_is_not_counted_when_no_window():
    # 今日開いたばかりのポジションは fetch_range を呼ばず、attempted にも含めない
    # （「確認すべき期間がない」のは失敗ではない）。
    positions = [
        {"symbol": "3993.T", "take_profit": 2989.0, "stop_loss": 2254.0, "opened_at": TODAY},
    ]
    with (
        patch("investment.jobs.morning_check.fetch_range") as fr,
        patch("investment.jobs.morning_check.record_gap") as gap,
    ):
        hits, failed, attempted = run(positions, conn=None, today=TODAY)

    fr.assert_not_called()
    gap.assert_not_called()
    assert hits == []
    assert failed == 0
    assert attempted == 0


# ---------------------------------------------------------------------------
# 修正2:「確認できなかった」を「売買はありませんでした」と表示しない。
# ---------------------------------------------------------------------------


def test_summarize_all_confirmed_no_hits():
    lines, code = summarize_result(positions_count=2, hits=[], failed=0, attempted=2)
    assert code == 0
    assert any("確認しました" in line and "売買はありませんでした" in line for line in lines)


def test_summarize_no_window_for_any_position_is_not_treated_as_failure():
    # 全銘柄が「まだ確認すべき期間がない」場合は、失敗ではなく通常の「売買なし」扱い
    lines, code = summarize_result(positions_count=1, hits=[], failed=0, attempted=0)
    assert code == 0
    assert any("売買はありませんでした" in line for line in lines)


def test_summarize_partial_fetch_failure_is_explicit_and_fails():
    lines, code = summarize_result(positions_count=5, hits=[], failed=2, attempted=5)
    assert code == 1
    joined = "\n".join(lines)
    assert "2" in joined
    assert "確認できませんでした" in joined


def test_summarize_total_fetch_failure_does_not_claim_no_trade():
    lines, code = summarize_result(positions_count=3, hits=[], failed=3, attempted=3)
    assert code == 1
    joined = "\n".join(lines)
    assert "確認できませんでした" in joined
    assert "売買はありませんでした" not in joined


def test_summarize_hits_present_returns_zero_even_with_partial_failures():
    # 到達した銘柄がある場合の終了コードは従来通り0（異常ではなく確認を促す正常な状態）
    hits = [
        {
            "symbol": "3993.T",
            "kind": "stop_loss",
            "estimated_price": 2254.0,
            "day_low": 2200.0,
            "day_high": 2500.0,
        }
    ]
    lines, code = summarize_result(positions_count=5, hits=hits, failed=2, attempted=5)
    assert code == 0
    joined = "\n".join(lines)
    assert "2" in joined  # 取得できなかった件数も明示する
