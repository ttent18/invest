"""買った/売った の申告を、保有と現金に反映する計算のテスト。

データベースには触れない純粋な計算なので、ここでのテストは速い。
"""

import pytest

from investment.config import bucket_by_name
from investment.fills import FillError, Position, apply_buy

PATIENT = bucket_by_name("じっくり")   # 利確 +22% / 損切り -8%
FAST = bucket_by_name("回転")          # 利確 +10% / 損切り -5%


def _fill(**overrides) -> dict:
    d = {
        "symbol": "1111.T",
        "side": "buy",
        "quantity": 100,
        "price": 900.0,
        "currency": "JPY",
        "fee": 0.0,
    }
    d.update(overrides)
    return d


def test_buying_a_stock_we_do_not_hold_creates_a_position():
    result = apply_buy(existing=None, fill=_fill(), rule=PATIENT, cash=550_000.0)

    assert result.position.symbol == "1111.T"
    assert result.position.quantity == 100
    assert result.position.avg_price == 900.0
    assert result.position.bucket == "じっくり"
    # 利確・損切りは、買値に枠の率を掛けた値
    assert result.position.take_profit == pytest.approx(1098.0)   # 900 × 1.22
    assert result.position.stop_loss == pytest.approx(828.0)      # 900 × 0.92


def test_buying_reduces_the_cash_by_the_cost_including_the_fee():
    result = apply_buy(existing=None, fill=_fill(fee=500.0), rule=PATIENT, cash=550_000.0)

    # 100株 × 900円 + 手数料500円 = 90,500円
    assert result.cash_delta == pytest.approx(-90_500.0)


def test_buying_records_a_trade_with_the_bucket():
    """あとで枠ごとの成績を出すために、取引に枠を残す。"""
    result = apply_buy(existing=None, fill=_fill(), rule=PATIENT, cash=550_000.0)

    assert result.trade["side"] == "buy"
    assert result.trade["symbol"] == "1111.T"
    assert result.trade["quantity"] == 100
    assert result.trade["price"] == 900.0
    assert result.trade["bucket"] == "じっくり"
    # 買った時点では損益も保有日数も決まらない
    assert result.trade["realized_pnl"] is None
    assert result.trade["holding_days"] is None


def test_buying_more_of_the_same_stock_averages_the_price():
    """買い増したら、平均取得単価を計算し直すこと。

    100株を900円で持っているところに、100株を1,100円で買い増すと、
    200株の平均は1,000円になる。
    """
    existing = Position(
        symbol="1111.T", quantity=100, avg_price=900.0, bucket="じっくり",
        take_profit=1098.0, stop_loss=828.0,
    )
    result = apply_buy(
        existing=existing, fill=_fill(price=1100.0), rule=PATIENT, cash=550_000.0
    )

    assert result.position.quantity == 200
    assert result.position.avg_price == pytest.approx(1000.0)
    # 利確・損切りも、新しい平均から計算し直す
    assert result.position.take_profit == pytest.approx(1220.0)   # 1000 × 1.22
    assert result.position.stop_loss == pytest.approx(920.0)      # 1000 × 0.92


def test_buying_more_in_a_different_bucket_is_rejected():
    """同じ銘柄を違う枠で持つことはできない。

    枠ごとに成績を測っているので、1つの保有が2つの枠にまたがると
    どちらの成績なのか決められなくなる。
    """
    existing = Position(
        symbol="1111.T", quantity=100, avg_price=900.0, bucket="じっくり",
        take_profit=1098.0, stop_loss=828.0,
    )
    with pytest.raises(FillError) as exc:
        apply_buy(existing=existing, fill=_fill(), rule=FAST, cash=550_000.0)

    assert "枠" in str(exc.value)


def test_buying_more_than_the_cash_allows_is_rejected():
    """現金が足りない買いは受け付けない。

    仮想資金の段階でも、現金がマイナスになると収支が意味を失う。
    """
    with pytest.raises(FillError) as exc:
        apply_buy(existing=None, fill=_fill(quantity=1000), rule=PATIENT, cash=50_000.0)

    assert "現金" in str(exc.value)
