from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch

from investment.jobs.morning_check import (
    LOOKBACK_DAYS,
    _lookback_window,
    detect_hits,
    find_expired,
    main,
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
    with (
        patch(
            "investment.jobs.morning_check.fetch_range", return_value=(2500.0, 2400.0)
        ) as fr,
        # run() は値動きが取れた銘柄について画面用の終値も取りに行くようになった。
        # 差し替えないと本物の yfinance に問い合わせに行ってしまう。
        patch("investment.jobs.morning_check.fetch_last_price", return_value=2450.0),
        patch("investment.jobs.morning_check.save_last_prices"),
    ):
        run(positions, conn=None, today=TODAY)

    fr.assert_called_once_with("3993.T", opened, TODAY - timedelta(days=1))


def test_run_detects_hit_found_anywhere_in_window():
    # 期間内のどこかで損切り価格に触れていれば検知する
    def fake_fetch(symbol, start, end):
        if symbol == "3993.T":
            return (2500.0, 2200.0)  # 安値が損切りを下回った
        return (200.0, 190.0)

    with (
        patch("investment.jobs.morning_check.fetch_range", side_effect=fake_fetch),
        patch("investment.jobs.morning_check.fetch_last_price", return_value=200.0),
        patch("investment.jobs.morning_check.save_last_prices"),
    ):
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
        patch("investment.jobs.morning_check.fetch_last_price", return_value=200.0),
        patch("investment.jobs.morning_check.save_last_prices"),
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


# --- 回転枠の期限切れ -------------------------------------------------------
# 回転枠は「10営業日で結論が出る」前提で入る。出なければ前提そのものが
# 外れているので、勝ち負けに関係なく降りて枠を空ける（rules/v3.md）。


def test_expired_lists_a_fast_bucket_position_past_its_deadline():
    """回転枠の銘柄が期限を過ぎていたら知らせること。"""
    positions = [
        # 8/24(月)に買って、今日は9/7(月)。営業日でちょうど10日経過
        {"symbol": "1111.T", "bucket": "回転", "opened_at": date(2026, 8, 24)},
    ]
    expired = find_expired(positions, today=date(2026, 9, 7))

    assert len(expired) == 1
    assert expired[0]["symbol"] == "1111.T"
    assert expired[0]["business_days"] == 10
    assert expired[0]["limit"] == 10


def test_expired_ignores_a_fast_bucket_position_still_within_the_deadline():
    positions = [
        # 9/1(火)に買って、今日は9/8(月)。営業日で5日経過
        {"symbol": "1111.T", "bucket": "回転", "opened_at": date(2026, 9, 1)},
    ]
    assert find_expired(positions, today=date(2026, 9, 8)) == []


def test_expired_ignores_the_patient_bucket_which_has_no_deadline():
    """じっくり枠には期限が無いので、どれだけ経っても対象外。"""
    positions = [
        {"symbol": "1111.T", "bucket": "じっくり", "opened_at": date(2026, 1, 1)},
    ]
    assert find_expired(positions, today=date(2026, 9, 8)) == []


def test_expired_counts_business_days_not_calendar_days():
    """土日を数えないこと。暦日で数えると、実際より早く期限切れになる。

    8/24(月)から9/4(金)は暦日で11日だが、営業日では9日でまだ期限内。
    ここを暦日で数えると、期限内の銘柄を売れと言ってしまう。
    """
    positions = [
        {"symbol": "1111.T", "bucket": "回転", "opened_at": date(2026, 8, 24)},
    ]
    assert find_expired(positions, today=date(2026, 9, 4)) == []


def test_expired_is_exactly_at_the_deadline_not_one_day_after():
    """期限ちょうどの日に降りること（1日ずれると、ルール文書と食い違う）。

    8/24(月)を起点に、9/4(金)は9営業日で期限内、9/7(月)が10営業日で期限。
    """
    positions = [
        {"symbol": "1111.T", "bucket": "回転", "opened_at": date(2026, 8, 24)},
    ]
    assert find_expired(positions, today=date(2026, 9, 4)) == []      # 9営業日
    assert len(find_expired(positions, today=date(2026, 9, 7))) == 1  # 10営業日


def test_summarize_lists_expired_positions_alongside_the_hits():
    """期限切れは「約定したかも」とは別のこととして、必ず表示すること。

    約定の可能性がある銘柄があってもなくても、期限切れは行動が要る。
    どちらかに埋もれさせない。
    """
    expired = [{"symbol": "1111.T", "bucket": "回転", "business_days": 11, "limit": 10}]
    lines, code = summarize_result(1, hits=[], failed=0, attempted=1, expired=expired)

    text = "\n".join(lines)
    assert "1111.T" in text
    assert "期限" in text
    assert "11" in text and "10" in text
    assert code == 0  # 期限切れ自体は異常ではない（行動を促す正常な状態）


def test_summarize_shows_expired_even_when_something_was_hit():
    expired = [{"symbol": "1111.T", "bucket": "回転", "business_days": 12, "limit": 10}]
    hits = [{"symbol": "2222.T", "kind": "take_profit", "estimated_price": 1464,
             "day_high": 1470, "day_low": 1400}]
    lines, _ = summarize_result(2, hits=hits, failed=0, attempted=2, expired=expired)

    text = "\n".join(lines)
    assert "2222.T" in text      # 約定の可能性
    assert "1111.T" in text      # 期限切れ


def test_summarize_says_nothing_about_deadlines_when_none_expired():
    lines, _ = summarize_result(1, hits=[], failed=0, attempted=1, expired=[])
    assert "期限" not in "\n".join(lines)


def test_main_notifies_when_something_may_have_been_executed():
    """損切り・利確に到達した可能性があるときは通知する。"""
    hits = [{"symbol": "1111.T", "kind": "stop_loss", "estimated_price": 828,
             "day_high": 900, "day_low": 820}]
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "じっくり",
                             "opened_at": date(2026, 9, 1)}]),
        patch("investment.jobs.morning_check.run", return_value=(hits, 0, 1)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_called_once()


def test_main_does_not_notify_on_a_quiet_morning():
    """何も起きていない朝は通知しない。"""
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "じっくり",
                             "opened_at": date(2026, 9, 1)}]),
        patch("investment.jobs.morning_check.run", return_value=([], 0, 1)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_not_called()


def test_main_notifies_when_nothing_could_be_confirmed():
    """値動きが1件も取れなかった朝も、確認できなかったことを知らせること。

    hits も expired も両方空になるため、以前はここで通知しないまま
    終わっていた。しかし「確認できていない」朝こそ、利用者が自分で
    SBI（証券会社）を見にいく必要がある朝である。
    """
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "じっくり",
                             "opened_at": date(2026, 9, 1)}]),
        patch("investment.jobs.morning_check.run", return_value=([], 1, 1)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_called_once()
    assert "確認できません" in notify.call_args.kwargs["body"]


def test_main_does_not_notify_when_no_position_had_a_window_to_check():
    """まだ確認すべき期間が無い（今日開いたばかり等）は失敗ではないので、通知しない。"""
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "じっくり",
                             "opened_at": date(2026, 9, 8)}]),
        patch("investment.jobs.morning_check.run", return_value=([], 0, 0)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_not_called()


def test_run_saves_the_prices_it_managed_to_fetch():
    """朝の確認が取れた株価を保存すること。画面が含み損益を出すのに使う。"""
    positions = [
        {"symbol": "1111.T", "bucket": "じっくり", "quantity": 100, "avg_price": 900,
         "take_profit": 1098, "stop_loss": 828, "opened_at": date(2026, 9, 1)},
        {"symbol": "2222.T", "bucket": "じっくり", "quantity": 100, "avg_price": 800,
         "take_profit": 976, "stop_loss": 736, "opened_at": date(2026, 9, 1)},
    ]

    def fake_range(symbol, start, end):
        if symbol == "2222.T":
            raise MarketDataError("取れません")
        return (920.0, 880.0)   # (高値, 安値)

    with (
        patch("investment.jobs.morning_check.fetch_range", side_effect=fake_range),
        # ブリーフ原文には無かったが、fetch_last_price を差し替えないと
        # 1111.T の終値を本物の yfinance へ問い合わせに行ってしまう
        # （実データへ接続するテストを書かないという方針に反する）。
        patch("investment.jobs.morning_check.fetch_last_price", return_value=910.0),
        # ブリーフ原文は record_gaps（複数形）をパッチしていたが、
        # morning_check.py が実際に呼ぶのは record_gap（単数形、値動きが
        # 1件取れなかったことをその場で記録する既存の関数）。record_gaps
        # という属性はモジュールに存在せず、そのままだと patch がここで
        # AttributeError になり、2222.T の失敗を記録しようとして
        # conn=None の本物の record_gap が呼ばれて落ちる（同じファイルの
        # test_run_counts_failures_and_records_gap も record_gap をパッチ
        # している）。誤記と判断し、実際に呼ばれる名前に合わせた。
        patch("investment.jobs.morning_check.record_gap"),
        patch("investment.jobs.morning_check.save_last_prices") as save,
    ):
        run(positions, conn=None, today=date(2026, 9, 8))

    # 取れた銘柄だけを渡す。取れなかった銘柄は含めない
    saved = save.call_args.args[1]
    assert list(saved) == ["1111.T"]


def test_main_notifies_when_a_position_passed_its_deadline():
    """回転枠の期限切れは、値動きが無くても知らせる。降りる必要があるため。

    opened_at を十分に古い日付にすることで、実際の「今日」が何日であっても
    確実に期限切れになる。「今日」を差し替えるモックは使わない
    （investment.jobs.morning_check.datetime を丸ごと置き換えると、
    date 単体で isinstance 判定している他のコードまで巻き込まれるため脆い）。
    """
    old = date(2020, 1, 1)
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "回転", "opened_at": old}]),
        patch("investment.jobs.morning_check.run", return_value=([], 0, 1)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_called_once()
