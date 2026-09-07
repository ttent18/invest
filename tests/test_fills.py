"""買った/売った の申告を、保有と現金に反映する計算のテスト。

データベースには触れない純粋な計算なので、ここでのテストは速い。
"""

from datetime import date

import pytest

from investment.config import bucket_by_name
from investment.fills import FillError, Position, apply_buy, apply_sell

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


def _held(**overrides) -> Position:
    d = {
        "symbol": "1111.T", "quantity": 100, "avg_price": 900.0,
        "bucket": "じっくり", "take_profit": 1098.0, "stop_loss": 828.0,
    }
    d.update(overrides)
    return Position(**d)


def test_selling_everything_removes_the_position():
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=1100.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    assert result.position is None      # 全部売ったので保有は消える


def test_selling_everything_adds_the_proceeds_to_the_cash():
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=1100.0, fee=300.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    # 100株 × 1,100円 - 手数料300円 = 109,700円
    assert result.cash_delta == pytest.approx(109_700.0)


def test_selling_records_the_profit_the_bucket_and_the_days_held():
    """枠ごとの成績を出すのに必要な3つを、売った時点で確定させる。"""
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=1100.0, fee=300.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    # (売値1,100円 - 平均取得900円) × 100株 - 手数料300円 = 19,700円
    assert result.trade["realized_pnl"] == pytest.approx(19_700.0)
    assert result.trade["bucket"] == "じっくり"
    assert result.trade["holding_days"] == 29
    assert result.trade["side"] == "sell"


def test_selling_at_a_loss_records_a_negative_profit():
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=828.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 10),
    )

    # (828 - 900) × 100 = -7,200円
    assert result.trade["realized_pnl"] == pytest.approx(-7_200.0)


def test_selling_part_of_a_position_keeps_the_rest_at_the_same_average():
    """一部だけ売ったら、残りの平均取得単価は変わらない。

    平均取得単価は「いくらで買ったか」なので、売っても動かない。
    """
    result = apply_sell(
        existing=_held(quantity=300),
        fill=_fill(side="sell", quantity=100, price=1100.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    assert result.position is not None
    assert result.position.quantity == 200
    assert result.position.avg_price == pytest.approx(900.0)
    assert result.position.bucket == "じっくり"


def test_selling_more_than_we_hold_is_rejected():
    with pytest.raises(FillError) as exc:
        apply_sell(
            existing=_held(quantity=100),
            fill=_fill(side="sell", quantity=200, price=1100.0),
            opened_at=date(2026, 9, 1),
            today=date(2026, 9, 30),
        )

    assert "保有" in str(exc.value)


def test_selling_a_stock_we_do_not_hold_is_rejected():
    with pytest.raises(FillError) as exc:
        apply_sell(
            existing=None,
            fill=_fill(side="sell", quantity=100, price=1100.0),
            opened_at=None,
            today=date(2026, 9, 30),
        )

    assert "保有していません" in str(exc.value)
