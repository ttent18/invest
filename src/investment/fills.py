"""利用者が申告した約定を、保有と現金にどう反映するかの計算。

外部に一切依存しない純粋な計算だけを置く。データベースへの書き込みは
investment.jobs.apply_fills が行う。分けている理由は、計算のテストに
データベースを用意しなくて済むようにするため。

**この計算は Python にしか無い。** 画面側（JavaScript）に同じ計算を書くと、
同じことが2箇所に存在して片方だけ直っていない状態が生まれる。
"""

from dataclasses import dataclass

from investment.config import Bucket


class FillError(RuntimeError):
    """申告された約定を反映できない。

    利用者の入力ミス（持っていない銘柄を売った、現金が足りない等）で起きる。
    黙って無視せず、理由を残して利用者に見せる。
    """


@dataclass(frozen=True)
class Position:
    """1銘柄の保有。"""

    symbol: str
    quantity: int
    avg_price: float
    bucket: str
    take_profit: float
    stop_loss: float


@dataclass(frozen=True)
class BuyResult:
    """買いを反映した結果。

    cash_delta は現金の増減（買いなので負の数）。
    trade はそのまま trades テーブルに入れる内容。
    """

    position: Position
    cash_delta: float
    trade: dict


def _exits(price: float, rule: Bucket) -> tuple[float, float]:
    """買値から、利確と損切りの価格を計算する。"""
    return price * (1 + rule.take_profit_pct), price * (1 - rule.stop_loss_pct)


def apply_buy(
    existing: Position | None, fill: dict, rule: Bucket, cash: float
) -> BuyResult:
    """買った申告を反映した結果を返す。

    existing はいまの保有（無ければ None）。rule は買った枠。
    cash は現在の現金残高で、足りるかの確認に使う。
    """
    cost = fill["quantity"] * fill["price"] + fill["fee"]
    if cost > cash:
        raise FillError(
            f"現金が足りません（必要 {cost:,.0f}円 / 残高 {cash:,.0f}円）"
        )

    if existing is None:
        quantity = fill["quantity"]
        avg_price = fill["price"]
    else:
        if existing.bucket != rule.name:
            raise FillError(
                f"{fill['symbol']} は既に{existing.bucket}枠で持っています。"
                f"同じ銘柄を{rule.name}枠でも持つことはできません"
                f"（どちらの枠の成績か決められなくなるため）"
            )
        quantity = existing.quantity + fill["quantity"]
        total_cost = (
            existing.quantity * existing.avg_price + fill["quantity"] * fill["price"]
        )
        avg_price = total_cost / quantity

    take_profit, stop_loss = _exits(avg_price, rule)

    return BuyResult(
        position=Position(
            symbol=fill["symbol"],
            quantity=quantity,
            avg_price=avg_price,
            bucket=rule.name,
            take_profit=take_profit,
            stop_loss=stop_loss,
        ),
        cash_delta=-cost,
        trade={
            "symbol": fill["symbol"],
            "side": "buy",
            "quantity": fill["quantity"],
            "price": fill["price"],
            "currency": fill["currency"],
            "fee": fill["fee"],
            "bucket": rule.name,
            "realized_pnl": None,
            "holding_days": None,
        },
    )
