from investment.config import BUCKETS, MAX_POSITIONS, SCREEN, SETTINGS, bucket_by_name


def test_settings_match_the_rules_document():
    assert SETTINGS.total_capital == 550_000
    assert SETTINGS.max_position_pct == 0.25
    assert SETTINGS.risk_per_trade_pct == 0.02
    assert SETTINGS.jp_fee_rate == 0.0
    assert SETTINGS.us_fee_rate == 0.01


def test_screen_criteria_match_the_rules_document():
    assert SCREEN.max_market_cap == 30_000_000_000
    assert SCREEN.min_revenue_growth == 0.20
    assert SCREEN.min_operating_margin == 0.10
    assert SCREEN.min_roe == 0.15
    assert SCREEN.min_equity_ratio == 0.40


# --- 枠（じっくり / 回転） ---------------------------------------------------


def test_there_are_two_buckets_with_two_slots_each():
    """4つの枠を、性格の違う2種類に2つずつ分ける。"""
    assert [b.name for b in BUCKETS] == ["じっくり", "回転"]
    assert [b.slots for b in BUCKETS] == [2, 2]
    assert MAX_POSITIONS == 4


def test_the_patient_bucket_keeps_the_v2_numbers():
    b = bucket_by_name("じっくり")
    assert (b.take_profit_pct, b.stop_loss_pct) == (0.22, 0.08)
    assert b.max_holding_days is None  # 期限なし


def test_the_fast_bucket_is_narrower_and_has_a_deadline():
    b = bucket_by_name("回転")
    assert (b.take_profit_pct, b.stop_loss_pct) == (0.10, 0.05)
    assert b.max_holding_days == 10  # 営業日。約2週間


def test_bucket_by_name_returns_none_for_an_unknown_name():
    assert bucket_by_name("なんとなく") is None


def test_every_bucket_stop_is_within_the_position_cap_assumption():
    """どの枠も損切り幅が8%以内であること。

    株数の上限は「1銘柄の金額上限」と「1回の損失上限」の小さいほうで決まる。
    損切り幅が 0.02 / 0.25 = 8% より広い枠を作ると、損失上限のほうが
    先に効くようになり、枠ごとに買える株数が変わる。
    build_context はいちばん広い損切り幅で株数を計算しているので
    安全側だが、この前提が変わったことに気づけるようにしておく。
    """
    threshold = SETTINGS.risk_per_trade_pct / SETTINGS.max_position_pct
    assert threshold == 0.08
    for b in BUCKETS:
        assert b.stop_loss_pct <= threshold, f"{b.name} の損切り幅が広すぎます"
