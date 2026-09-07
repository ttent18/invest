"""利用者が申告した約定を、保有・現金・取引履歴に反映する。

スマホの画面から「買った」を押すと、その内容が fills テーブルに
1行そのまま入る。画面側は計算をしない。計算はこのジョブだけが行う。

同じ計算を画面側（JavaScript）にも書くと、同じことが2箇所に存在して
片方だけ直っていない状態が生まれる。それを避けるための分担である。
"""

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from investment.config import bucket_by_name
from investment.db import (
    connect,
    mark_fill_failed,
    save_fill_result,
    select_capital,
    select_cash,
    select_positions,
    select_unapplied_fills,
)
from investment.fills import FillError, Position, apply_buy, apply_sell

JST = ZoneInfo("Asia/Tokyo")


def _position_of(conn, symbol: str) -> tuple[Position | None, date | None]:
    """いまの保有と、最初に持った日を返す。"""
    for p in select_positions(conn):
        if p["symbol"] == symbol:
            opened = p["opened_at"]
            return (
                Position(
                    symbol=p["symbol"],
                    quantity=int(p["quantity"]),
                    avg_price=float(p["avg_price"]),
                    bucket=p["bucket"],
                    take_profit=float(p["take_profit"]),
                    stop_loss=float(p["stop_loss"]),
                ),
                opened.astimezone(JST).date() if opened is not None else None,
            )
    return None, None


def _bucket_of_proposal(conn, proposal_id) -> str | None:
    if proposal_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT bucket FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
    return row["bucket"] if row else None


def _mark_proposal_taken(conn, proposal_id) -> None:
    if proposal_id is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE proposals SET outcome = 'taken' WHERE id = %s", (proposal_id,)
        )
    conn.commit()


def run(conn, today: date) -> tuple[int, int]:
    """未反映の申告を古い順に反映する。戻り値は (反映できた件数, できなかった件数)。

    1件の失敗で全体を止めない。失敗した申告は消さず、理由を残す。
    """
    ok = failed = 0
    for row in select_unapplied_fills(conn):
        fill = {
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": int(row["quantity"]),
            "price": float(row["price"]),
            "currency": row["currency"],
            "fee": float(row["fee"]),
        }
        existing, opened_at = _position_of(conn, fill["symbol"])

        try:
            if fill["side"] == "buy":
                bucket_name = _bucket_of_proposal(conn, row["proposal_id"])
                rule = bucket_by_name(bucket_name or "")
                if rule is None:
                    raise FillError(
                        "この買いがどの枠のものか分かりません"
                        "（提案に紐づいていないか、知らない枠の名前です）"
                    )
                cash = select_cash(conn).get(fill["currency"], 0.0)
                result = apply_buy(existing, fill, rule, cash)
            else:
                result = apply_sell(existing, fill, opened_at, today)

            save_fill_result(
                conn,
                row["id"],
                result.position,
                result.cash_delta,
                result.trade,
                fill["currency"],
            )
        except FillError as exc:
            # save_fill_result が現金の反映に失敗して例外を出した場合、
            # そこまでに書いた取引・保有はまだコミットされていない。
            # ここで rollback しないと、直後の mark_fill_failed の commit が
            # その未確定分もろとも確定させてしまい、「現金だけ動かない」
            # という一番避けたい壊れ方になる。
            conn.rollback()
            failed += 1
            mark_fill_failed(conn, row["id"], str(exc))
            print(f"反映できません #{row['id']} {fill['symbol']}: {exc}")
            continue

        if fill["side"] == "buy":
            _mark_proposal_taken(conn, row["proposal_id"])
        ok += 1
        print(
            f"反映しました #{row['id']} {fill['symbol']} {fill['side']} "
            f"{fill['quantity']}株 × {fill['price']:,.0f}円"
        )

    return ok, failed


def main() -> int:
    today = datetime.now(tz=JST).date()
    with connect() as conn:
        ok, failed = run(conn, today)
        capital = select_capital(conn)

    print(f"反映 {ok} 件 / 反映できず {failed} 件 / 総資金 {capital:,.0f}円")
    # 反映できなかったものがあっても、ジョブ自体は成功とする。
    # 理由は fills に残っており、画面に出るため。ここで失敗にすると、
    # 利用者の入力ミス1件でワークフロー全体が赤くなる。
    return 0


if __name__ == "__main__":
    sys.exit(main())
