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


def _proposal_for(conn, proposal_id) -> dict | None:
    """申告に紐づく提案の内容（銘柄・枠・実行済みかどうか）を返す。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT symbol, bucket, outcome FROM proposals WHERE id = %s", (proposal_id,)
        )
        row = cur.fetchone()
    return dict(row) if row else None


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
            # 申告が提案に紐づいている場合、その提案がこの申告と本当に
            # 対応しているかをここで確かめる。確かめないと2つの事故が
            # 起きる。
            #
            # 1. 画面で違うカードを押した場合。A社の買いをB社の提案に
            #    紐づけて記録すると、そのまま反映してしまえばB社の提案が
            #    「実行した」ことになり、A社の提案は pending のまま残って
            #    もう一度買う提案として出てしまう。
            # 2. 同じ提案に対する申告が2件ある場合（二重に記録した、
            #    画面を2回タップした等）。1件目で提案は「実行した」に
            #    なっているので、2件目をそのまま反映すると同じ売買が
            #    二重に保有・現金へ反映されてしまう。
            proposal = None
            if row["proposal_id"] is not None:
                proposal = _proposal_for(conn, row["proposal_id"])
                if proposal is None:
                    raise FillError(f"紐づいている提案(#{row['proposal_id']})が見つかりません")
                if proposal["symbol"] != fill["symbol"]:
                    raise FillError(
                        f"申告した銘柄（{fill['symbol']}）と、紐づいている提案の銘柄"
                        f"（{proposal['symbol']}）が一致しません。"
                        "画面で違う銘柄のカードを押した可能性があります"
                    )
                if proposal["outcome"] == "taken":
                    raise FillError(
                        f"紐づいている提案(#{row['proposal_id']})は既に実行済みとして"
                        "記録されています。同じ売買を二重に申告した可能性があります"
                    )

            if fill["side"] == "buy":
                rule = bucket_by_name(proposal["bucket"]) if proposal else None
                if rule is None:
                    raise FillError(
                        "この買いがどの枠のものか分かりません"
                        "（提案に紐づいていないか、知らない枠の名前です）"
                    )
                cash = select_cash(conn).get(fill["currency"], 0.0)
                result = apply_buy(existing, fill, rule, cash)
            else:
                result = apply_sell(existing, fill, opened_at, today)

            # 申告に提案が紐づいていれば、同じトランザクションの中で
            # その提案も「実行した」（outcome = 'taken'）にする。買い・
            # 売りのどちらでも、反映だけ終わって提案が pending のまま
            # 残ると、スマホの画面に「まだ実行していない提案」として
            # 同じ銘柄がまた出てしまい、二重に売買する事故につながる。
            # 提案に紐づいていない申告（row["proposal_id"] が None）は
            # save_fill_result 側で何もしないので、ここで場合分けしない。
            save_fill_result(
                conn,
                row["id"],
                result.position,
                result.cash_delta,
                result.trade,
                fill["currency"],
                row["recorded_at"],
                proposal_id=row["proposal_id"],
            )
        except FillError as exc:
            # save_fill_result が現金の反映などに失敗して例外を出した場合、
            # そこまでに書いた取引・保有・提案の更新はまだコミットされて
            # いない。ここで rollback しないと、直後の mark_fill_failed の
            # commit がその未確定分もろとも確定させてしまい、「現金だけ
            # 動かない」という一番避けたい壊れ方になる。
            conn.rollback()
            failed += 1
            mark_fill_failed(conn, row["id"], str(exc))
            print(f"反映できません #{row['id']} {fill['symbol']}: {exc}")
            continue

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
