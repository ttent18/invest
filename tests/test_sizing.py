import pytest

from investment.sizing import position_size, required_win_rate


def test_required_win_rate_oneil_no_fee():
    # 買値100、利確122(+22%)、損切り92(-8%)、手数料なし
    # 必要勝率 = 8 / (22 + 8) = 0.2667
    assert required_win_rate(100, 122, 92, 0.0) == pytest.approx(0.2667, abs=0.0005)


def test_required_win_rate_one_to_one():
    # +8% / -8% は必要勝率 50%
    assert required_win_rate(100, 108, 92, 0.0) == pytest.approx(0.50, abs=0.0005)


def test_required_win_rate_us_fee_raises_the_bar():
    # 米国株は往復1%。+22%/-8% でも必要勝率が上がる
    without_fee = required_win_rate(100, 122, 92, 0.0)
    with_fee = required_win_rate(100, 122, 92, 0.01)
    assert with_fee > without_fee
    # 実効 +21% / -9% → 9 / 30 = 0.30
    assert with_fee == pytest.approx(0.30, abs=0.0005)


def test_required_win_rate_returns_one_when_fee_eats_the_gain():
    # +1%狙いで往復1%の手数料 → 利益が残らない
    assert required_win_rate(100, 101, 92, 0.01) == 1.0


def test_required_win_rate_rejects_invalid_order():
    with pytest.raises(ValueError):
        required_win_rate(100, 90, 92, 0.0)  # 利確が買値より下


def test_position_size_limited_by_risk():
    # 資金55万、1取引の損失許容2%=11,000円
    # 買値2450、損切り2254 → 1株あたりの損失196円
    # 11000 / 196 = 56.1 → 56株
    # 上限15% = 82,500円 / 2450 = 33.6 → 33株
    # 小さいほうを取るので 33株
    assert position_size(550_000, 0.02, 0.15, 2450, 2254) == 33


def test_position_size_limited_by_risk_when_stop_is_wide():
    # 損切り幅を広くすると、リスク側が効く
    # 買値2450、損切り1960 → 1株490円
    # 11000 / 490 = 22.4 → 22株（上限33株より小さい）
    assert position_size(550_000, 0.02, 0.15, 2450, 1960) == 22


def test_position_size_returns_zero_when_one_share_is_too_expensive():
    # 1株の価格が上限を超える場合は0株
    assert position_size(550_000, 0.02, 0.15, 100_000, 92_000) == 0


def test_position_size_rejects_invalid_stop():
    with pytest.raises(ValueError):
        position_size(550_000, 0.02, 0.15, 2450, 2450)
