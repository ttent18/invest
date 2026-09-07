"""Neon (Postgres) への接続とクエリ。

記録の整合性を保つため、複数テーブルにまたがる更新は必ずトランザクションで行う。
"""

import os
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

from investment.config import BUCKETS, ScreenCriteria
from investment.fills import FillError
from investment.market import Fundamentals
from investment.sizing import required_win_rate

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


@contextmanager
def connect(url: str | None = None):
    """接続を開く。url を省略した場合は環境変数 DATABASE_URL を使う。"""
    load_dotenv()
    dsn = url or os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL が設定されていません")
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        yield conn


def apply_migrations(conn) -> None:
    """migrations/ の .sql を名前順に適用する。何度実行しても安全。"""
    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        with conn.cursor() as cur:
            cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def upsert_fundamentals(conn, rows: list[Fundamentals], as_of: date) -> int:
    """ファンダメンタルズを保存する。同じ日の同じ銘柄は上書きする。"""
    sql = """
        INSERT INTO fundamentals
            (symbol, as_of, name, market_cap, revenue_growth,
             operating_margin, roe, equity_ratio)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (symbol, as_of) DO UPDATE SET
            name = EXCLUDED.name,
            market_cap = EXCLUDED.market_cap,
            revenue_growth = EXCLUDED.revenue_growth,
            operating_margin = EXCLUDED.operating_margin,
            roe = EXCLUDED.roe,
            equity_ratio = EXCLUDED.equity_ratio
    """
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    f.symbol, as_of, f.name, f.market_cap, f.revenue_growth,
                    f.operating_margin, f.roe, f.equity_ratio,
                )
                for f in rows
            ],
        )
    conn.commit()
    return len(rows)


def select_screened(conn, criteria: ScreenCriteria, limit: int) -> list[dict]:
    """最新の取得日で、スクリーニング条件を満たす銘柄を返す。

    NULL の項目は通さない。SQL の比較で NULL は偽になるため、
    明示的に IS NOT NULL を書かなくても除外されるが、意図を示すため書く。
    """
    sql = """
        SELECT * FROM fundamentals
        WHERE as_of = (SELECT MAX(as_of) FROM fundamentals)
          AND market_cap       IS NOT NULL AND market_cap       <= %s
          AND revenue_growth   IS NOT NULL AND revenue_growth   >= %s
          AND operating_margin IS NOT NULL AND operating_margin >= %s
          AND roe              IS NOT NULL AND roe              >= %s
          AND equity_ratio     IS NOT NULL AND equity_ratio     >= %s
        ORDER BY revenue_growth DESC
        LIMIT %s
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                criteria.max_market_cap,
                criteria.min_revenue_growth,
                criteria.min_operating_margin,
                criteria.min_roe,
                criteria.min_equity_ratio,
                limit,
            ),
        )
        return list(cur.fetchall())


def record_gaps(conn, gaps: list[tuple[str, str]]) -> int:
    """データが取れなかった事実をまとめて残す。取れたことにしない。

    gaps は (scope, detail) の組のリスト。何千件になっても書き込みは1回で
    済ませる。1件ずつ接続を往復させると、件数が多いときに時間がかかり、
    その間に Neon 側から接続を切られる恐れがあるため。
    """
    if not gaps:
        return 0
    with conn.cursor() as cur:
        cur.executemany(
            "INSERT INTO data_gaps (occurred_at, scope, detail) VALUES (NOW(), %s, %s)",
            gaps,
        )
    conn.commit()
    return len(gaps)


def record_gap(conn, scope: str, detail: str) -> None:
    """データが取れなかった事実を1件残す。取れたことにしない。"""
    record_gaps(conn, [(scope, detail)])


def select_push_subscriptions(conn) -> list[dict]:
    """通知の宛先を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def delete_push_subscription(conn, endpoint: str) -> None:
    """失効した宛先を消す。

    通知サーバーが「その宛先はもう無い」と答えたときだけ呼ぶ。
    残したまま送り続けると毎回失敗が記録され、本当の失敗が埋もれる。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
    conn.commit()


def select_positions(conn) -> list[dict]:
    """現在の保有を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM positions ORDER BY symbol")
        return list(cur.fetchall())


def select_cash(conn) -> dict[str, float]:
    """通貨ごとの現金残高を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT currency, amount FROM cash")
        return {r["currency"]: float(r["amount"]) for r in cur.fetchall()}


def select_capital(conn) -> float:
    """いまの総資金を返す。現金の残高 ＋ 保有の取得原価。

    現在の株価は使わない。理由は2つ。

    1. 現在値を使うと、含み益が出ているだけで次に買う金額が膨らみ、
       リスクが勝手に増えてしまう
    2. 現在値の取得は通信が必要で失敗しうる。総資金の計算が
       通信の失敗で止まるのは筋が悪い

    利確して現金が増えれば総資金も増えるので、
    「利確して資金を増やし、さらに投資する」という循環は成立する。

    いまは円だけを数える。ドルを円に足すと桁が狂うため。
    米国株を有効にするときに、為替を掛けて足す形へ直すこと。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS c FROM cash WHERE currency = 'JPY'")
        cash = float(cur.fetchone()["c"])
        cur.execute(
            """
            SELECT COALESCE(SUM(quantity * avg_price), 0) AS c
            FROM positions WHERE currency = 'JPY'
            """
        )
        held = float(cur.fetchone()["c"])
    return cash + held


def init_cash(conn, jpy: float) -> int:
    """現金残高を初期化する。何度実行しても安全（冪等）。

    JPY を指定額、USD を 0 で初期化する。通貨ごとに ON CONFLICT DO NOTHING を
    使うため、既に残高がある通貨（取引などで変動済みのもの）には一切触れず、
    上書きしない。戻り値は新規に挿入した通貨の件数（0〜2）。
    """
    inserted = 0
    with conn.cursor() as cur:
        for currency, amount in (("JPY", jpy), ("USD", 0)):
            cur.execute(
                """
                INSERT INTO cash (currency, amount) VALUES (%s, %s)
                ON CONFLICT (currency) DO NOTHING
                """,
                (currency, amount),
            )
            inserted += cur.rowcount
    conn.commit()
    return inserted


def select_unapplied_fills(conn) -> list[dict]:
    """まだ保有・現金に反映していない申告を、古い順に返す。

    古い順に処理しないと、買う前に売ることになって反映に失敗する。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fills WHERE applied_at IS NULL ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def mark_fill_failed(conn, fill_id: int, reason: str) -> None:
    """反映できなかった理由を記録する。行は消さず、未反映のまま残す。

    消してしまうと、利用者は「記録したはずなのに無い」という状態に置かれ、
    原因も分からなくなる。
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fills SET apply_error = %s WHERE id = %s", (reason, fill_id)
        )
    conn.commit()


def save_fill_result(
    conn,
    fill_id: int,
    position,
    cash_delta: float,
    trade: dict,
    currency: str,
    recorded_at,
    proposal_id: int | None = None,
) -> None:
    """1件の申告の反映を、まとめて1つのトランザクションで書き込む。

    取引の追加・保有の更新・現金の増減・申告を反映済みにする・
    （該当すれば）提案を実行済みにする、の5つは途中で止まると
    帳尻が合わなくなるため、必ず全部成功か全部取り消しにする。

    position が None なら、その銘柄の保有を削除する（全部売った場合）。

    recorded_at は利用者が申告した時刻（fills.recorded_at）。取引の
    executed_at と保有の opened_at にはこれを使い、ジョブが実行された時刻
    （NOW()）は使わない。月曜の場中に買ってもジョブは翌朝に実行されるため、
    NOW() を使うと opened_at が翌朝になり、保有日数（回転枠の期限判定や
    枠ごとの成績の計算に使う）が実際よりずれてしまう。

    proposal_id を渡すと、その提案も同じトランザクションで「実行した」
    （outcome = 'taken'）にする。反映と別のトランザクションにすると、
    反映は終わったのに提案だけ pending のまま残る隙間ができ、
    スマホの画面に「まだ買っていない提案」として同じ銘柄が
    再び出て、二重に買う事故につながる。proposal_id が None（提案に
    紐づかない申告）の場合は、実行済みにする提案が無いので何もしない。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency, fee,
                 bucket, realized_pnl, holding_days)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                recorded_at, trade["symbol"], trade["side"], trade["quantity"],
                trade["price"], trade["currency"], trade["fee"], trade["bucket"],
                trade["realized_pnl"], trade["holding_days"],
            ),
        )

        if position is None:
            cur.execute("DELETE FROM positions WHERE symbol = %s", (trade["symbol"],))
        else:
            # opened_at は ON CONFLICT の更新対象に入れない。買い増しても
            # 「最初に持った日」を動かさないため。動かすと回転枠の期限
            # （10営業日）が買い増すたびにリセットされてしまう。
            cur.execute(
                """
                INSERT INTO positions
                    (symbol, quantity, avg_price, currency, take_profit, stop_loss,
                     opened_at, bucket)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (symbol) DO UPDATE SET
                    quantity    = EXCLUDED.quantity,
                    avg_price   = EXCLUDED.avg_price,
                    take_profit = EXCLUDED.take_profit,
                    stop_loss   = EXCLUDED.stop_loss,
                    bucket      = EXCLUDED.bucket
                """,
                (
                    position.symbol, position.quantity, position.avg_price, currency,
                    position.take_profit, position.stop_loss, recorded_at,
                    position.bucket,
                ),
            )

        cur.execute(
            "UPDATE cash SET amount = amount + %s WHERE currency = %s",
            (cash_delta, currency),
        )
        # この通貨の行が cash に無いと、UPDATE は1行も変えずに終わり、
        # 何もエラーが起きないまま現金だけが反映されない状態になる。
        # rowcount で更新できたか必ず確認し、0行なら例外にしてトランザクション
        # 全体を取り消す（fills も未反映のまま残るので、あとで気づける）。
        if cur.rowcount == 0:
            raise FillError(
                f"現金（{currency}）の残高が登録されていません。"
                "init_cash などで先に現金の行を作ってください"
            )
        cur.execute(
            "UPDATE fills SET applied_at = NOW(), apply_error = NULL WHERE id = %s",
            (fill_id,),
        )
        if proposal_id is not None:
            cur.execute(
                "UPDATE proposals SET outcome = 'taken' WHERE id = %s", (proposal_id,)
            )
    conn.commit()


def select_bucket_performance(conn) -> list[dict]:
    """枠ごとの成績を返す。「どちらの型が向いているか」を測るための表。

    数えるのは売って決着した取引だけ。買っただけの分は勝ち負けが
    決まっていないので含めない。

    勝率だけでは判断できないので、損益トントンの勝率も一緒に返す。
    それを上回っていて初めて「効いている」と言える。

    まだ1件も決着していない枠も 0 件として返す。行が消えると
    「まだ始まっていない」のか「集計から漏れた」のか分からなくなる。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bucket,
                   COUNT(*)                                  AS closed,
                   COUNT(*) FILTER (WHERE realized_pnl > 0)  AS wins,
                   COALESCE(SUM(realized_pnl), 0)             AS total_pnl,
                   AVG(holding_days)                          AS avg_holding_days
            FROM trades
            WHERE side = 'sell' AND bucket IS NOT NULL
            GROUP BY bucket
            """
        )
        by_name = {r["bucket"]: r for r in cur.fetchall()}

    result = []
    for b in BUCKETS:
        row = by_name.get(b.name)
        closed = int(row["closed"]) if row else 0
        wins = int(row["wins"]) if row else 0
        entry = 1000.0  # 率だけを求めるので、基準の値は何でもよい
        result.append(
            {
                "bucket": b.name,
                "closed": closed,
                "wins": wins,
                "win_rate": (wins / closed) if closed else None,
                "total_pnl": float(row["total_pnl"]) if row else 0.0,
                "avg_holding_days": (
                    float(row["avg_holding_days"])
                    if row and row["avg_holding_days"] is not None
                    else None
                ),
                "breakeven_win_rate": required_win_rate(
                    entry,
                    entry * (1 + b.take_profit_pct),
                    entry * (1 - b.stop_loss_pct),
                    0.0,
                ),
            }
        )
    return result
