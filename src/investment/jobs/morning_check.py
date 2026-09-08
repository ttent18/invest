"""毎朝、直近の値動きから約定した可能性のある銘柄を洗い出す。

SBI証券には参照系のAPIがなく、米国株は約定通知メールも来ない。
そのため直近の高値・安値と、SBIに置いた利確・損切りの値を突き合わせて推定する。

「前日」だけを見ると、月曜朝（前日=日曜）や祝日明けの朝（前日=休場日）に
値動きが一件も取得できず、判定が必ず空になる。このとき「売買はありませんでした」
と表示してしまうと、「確認したが何も起きていなかった」のか「そもそも確認できて
いない」のかが利用者から区別できない。後者を前者と混同すると、SBI側で実際には
損切りが成立していても気づかないまま日が過ぎてしまう。
そのため、直近の一定期間（LOOKBACK_DAYS）を遡って値動きを確認する。
"""

import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from investment.config import bucket_by_name
from investment.db import connect, record_gap, save_last_prices, select_positions
from investment.market import MarketDataError, fetch_last_price, fetch_range
from investment.notify import send as notify_send

JST = ZoneInfo("Asia/Tokyo")  # 「今日」は日本時間で決める（GitHub Actions は UTC で動くため）


def _record_gap_safely(conn, scope: str, detail: str) -> None:
    """record_gap（「取れなかった事実を残す」だけの補助）自体の失敗で、
    本体（損切り・利確への到達の確認と通知）を巻き添えにしない。

    record_gap はもともと「値動きが取れなかった」等の“補助的な事実”を残す
    ためだけの処理で、それ自体が失敗する（データベース接続の問題など）と、
    run() が例外で止まり、まだ判定していない残りの保有の確認や、既に
    見つかっている損切り到達の通知まで丸ごと飛ばなくなってしまう。
    これはこのプロジェクトで4回目の同じ型の欠陥（notify.send → 分析全体、
    1件の申告の失敗 → 残り全部、株価の保存の失敗 → 損切り通知、
    そして今回は record_gap 自体 → 損切り通知、を巻き添えにしていた）。

    ここは「本体を絶対に落とさない」ことが要件なので、想定外の例外も
    含めて広く受け止める。狭く捕まえる利点（想定外の型に気づけること）は
    失われるが、その失敗を record_gap に書くことはできない（それ自体が
    失敗しているため）ので、代わりにログ（標準出力）に必ず残すことで
    黙って消えないようにする。
    """
    try:
        record_gap(conn, scope=scope, detail=detail)
    except Exception as exc:  # noqa: BLE001 - 理由は上のコメントを参照
        print(f"gap を記録できませんでした（{scope}）: {exc}")


# 遡る暦日数。営業日カレンダー（祝日データ）は持ち込まず、暦日で一定期間を遡る。
# 日本には3連休（土日+祝日、祝日+土日、祝日+土日+祝日）が多くあり、
# 単純に「前日」だけを見ると休場日の朝は必ず判定漏れになる。
# 5日あれば、金〜月の3連休や、木の祝日+土日のような4連休をまたいでも
# 直近の取引日の値動きまで遡って拾える。
LOOKBACK_DAYS = 5


def _lookback_window(opened_at: date, today: date) -> tuple[date, date] | None:
    """値動きを確認すべき期間 (開始日, 終了日) を返す。

    開始日は opened_at より前にしない。買う前の値動きを拾うと、
    実際には起きていない約定を「起きたはず」と誤検知してしまう。
    終了日は前日（今日はまだ取引が終わっていない可能性があるため含めない）。
    確認すべき期間が存在しない場合（今日開いたばかりのポジションなど）は None。
    """
    end = today - timedelta(days=1)
    start = max(opened_at, today - timedelta(days=LOOKBACK_DAYS))
    if start > end:
        return None
    return start, end


def _opened_date(opened_at, today: date) -> date:
    """positions.opened_at (date または timezone付きdatetime) を日本時間の日付にする。"""
    if isinstance(opened_at, datetime):
        if opened_at.tzinfo is not None:
            opened_at = opened_at.astimezone(JST)
        return opened_at.date()
    if isinstance(opened_at, date):
        return opened_at
    return today  # 想定外の型は「今日開いた」扱いにして安全側に倒す


def _business_days_between(start: date, end: date) -> int:
    """start から end までの営業日数を数える（土日を除く。祝日は考慮しない）。

    祝日カレンダーを持ち込まないのは、外部データへの依存を増やしたくないため。
    祝日を数えてしまうぶん実際よりわずかに長く数えるが、期限の判定が
    「少し長めに待つ」側にずれるだけなので、早すぎる期限切れは起きない。
    """
    days = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 月〜金
            days += 1
    return days


def find_expired(positions: list[dict], today: date) -> list[dict]:
    """期限を過ぎた保有を返す（回転枠のみ。じっくり枠には期限が無い）。

    回転枠は「10営業日で結論が出る」前提で入る。出なければ前提そのものが
    外れているので、勝ち負けに関係なく降りて枠を空ける（rules/v3.md）。
    値動きしない銘柄が枠に居座ると、4枠しかない戦力が25%減ったまま戻らない。
    """
    expired = []
    for p in positions:
        bucket = bucket_by_name(p.get("bucket") or "")
        if bucket is None or bucket.max_holding_days is None:
            continue
        opened = _opened_date(p["opened_at"], today)
        elapsed = _business_days_between(opened, today)
        # 「10営業日で手仕舞い」なので、ちょうど10営業日たった日に降りる。
        # > にすると11営業日目まで持つことになり、ルール文書と1日ずれる。
        if elapsed >= bucket.max_holding_days:
            expired.append({
                "symbol": p["symbol"],
                "bucket": bucket.name,
                "business_days": elapsed,
                "limit": bucket.max_holding_days,
            })
    return expired


def detect_hits(
    positions: list[dict], ranges: dict[str, tuple[float, float]]
) -> list[dict]:
    """利確または損切りに触れた銘柄を返す。

    値動きが取れなかった銘柄は判定しない（「触れていない」と扱わない）。
    損切りと利確の両方に触れた場合は、より不利な損切りを優先する。
    """
    hits: list[dict] = []
    for p in positions:
        rng = ranges.get(p["symbol"])
        if rng is None:
            continue
        high, low = rng
        if low <= float(p["stop_loss"]):
            hits.append({
                "symbol": p["symbol"], "kind": "stop_loss",
                "estimated_price": float(p["stop_loss"]), "day_low": low, "day_high": high,
            })
        elif high >= float(p["take_profit"]):
            hits.append({
                "symbol": p["symbol"], "kind": "take_profit",
                "estimated_price": float(p["take_profit"]), "day_low": low, "day_high": high,
            })
    return hits


def run(positions: list[dict], conn, today: date) -> tuple[list[dict], int, int]:
    """保有銘柄ごとに直近の値動きを取得し、判定する。

    戻り値: (約定した可能性のある銘柄, 値動きが取得できなかった件数, 判定対象になった件数)。
    「判定対象になった件数」(attempted) は、確認すべき期間がある銘柄の数。
    まだ確認すべき期間がない銘柄（今日開いたばかりなど）は含めない
    （取得を試みてすらいないので、失敗としてもカウントしない）。

    値動きの取得に成功した銘柄については、画面に含み損益を出すための終値も
    ついでに取って保存する（Cloudflare 側からは株価を取れないため、
    ここで見た値を保存しておく）。終値の取得に失敗しても、損切り・利確に
    到達した可能性の判定は続ける（そちらのほうが重要なため）。
    """
    ranges: dict[str, tuple[float, float]] = {}
    last_prices: dict[str, float] = {}
    failed = 0
    attempted = 0
    for p in positions:
        opened = _opened_date(p["opened_at"], today)
        window = _lookback_window(opened, today)
        if window is None:
            continue
        start, end = window
        attempted += 1
        try:
            ranges[p["symbol"]] = fetch_range(p["symbol"], start, end)
        except MarketDataError as exc:
            failed += 1
            _record_gap_safely(conn, scope=f"daily_range:{p['symbol']}", detail=str(exc))
            continue

        # 値動きの取得に成功した銘柄について、画面に出す用の終値も取る。
        # ここが失敗しても、売れた可能性の判定は続ける（通知のほうが重要）。
        try:
            last_prices[p["symbol"]] = fetch_last_price(p["symbol"])
        except MarketDataError:
            pass

    hits = detect_hits(positions, ranges)

    # 株価の保存に失敗しても、損切り・利確への到達を知らせるほうを止めない。
    # 保存できたかどうかは画面の見た目の話で、通知は「あなたが動く必要がある」
    # という知らせ。軽いほうの失敗で重いほうを巻き添えにしない。
    # 失敗した事実は記録に残すので、黙って消えることはない。
    try:
        save_last_prices(conn, last_prices)
    except Exception as exc:  # noqa: BLE001 - 理由は上のコメントを参照
        _record_gap_safely(
            conn,
            scope="price:save_failed",
            detail=f"最後に見た株価を保存できませんでした: {exc}",
        )

    return hits, failed, attempted


def summarize_result(
    positions_count: int, hits: list[dict], failed: int, attempted: int,
    expired: list[dict] | None = None,
) -> tuple[list[str], int]:
    """判定結果を、表示する行と終了コードに変換する。

    「確認できなかった」（値動きが取得できなかった）ことと「確認したが売買はなかった」
    ことを、はっきり別のメッセージにする。前者を後者のように見せてしまうと、
    利用者は本来必要な確認をしないまま放置してしまう。

    値動きが取得できなかった銘柄が1件以上あり、かつ約定の可能性がある銘柄が
    一つもない場合は、GitHub Actions で失敗として見えるよう終了コード1を返す
    （静かに成功扱いにしない）。到達した銘柄がある場合は、それ自体が
    人間に確認を促す正常な状態なので、終了コードは0のままでよい。
    """
    lines: list[str] = []

    # 期限切れは「約定したかも」とは別の話で、どちらであっても行動が要る。
    # 片方に埋もれないよう、先に独立した固まりとして出す。
    for e in expired or []:
        lines.append(
            f"  {e['symbol']}  {e['bucket']}枠の期限切れ"
            f"（{e['business_days']}営業日経過 / 期限{e['limit']}営業日）。"
            f"値動きに関係なく、降りて枠を空けてください"
        )
    if expired:
        lines.insert(0, f"次の {len(expired)} 件は保有の期限を過ぎています:")
        lines.append("")

    if hits:
        lines.append(f"次の {len(hits)} 件は約定した可能性があります。SBIで確認してください:")
        for h in hits:
            label = "損切り" if h["kind"] == "stop_loss" else "利確"
            lines.append(
                f"  {h['symbol']}  {label} {h['estimated_price']} に到達"
                f"（直近 高値 {h['day_high']} / 安値 {h['day_low']}）"
            )
        if failed:
            lines.append(f"なお、{failed} 件は値動きが取得できず、確認できませんでした")
        return lines, 0

    if attempted == 0 or failed == 0:
        lines.append(f"保有 {positions_count} 件。確認しました。売買はありませんでした")
        return lines, 0

    if failed == attempted:
        lines.append(f"保有 {positions_count} 件について値動きが取得できず、確認できませんでした")
        return lines, 1

    checked = attempted - failed
    lines.append(
        f"保有 {positions_count} 件のうち {checked} 件は確認しました（売買はありませんでした）。"
        f"{failed} 件は値動きが取得できず、確認できませんでした"
    )
    return lines, 1


def main() -> int:
    today = datetime.now(tz=JST).date()
    with connect() as conn:
        positions = select_positions(conn)
        if not positions:
            print("保有なし。確認は不要です")
            return 0

        hits, failed, attempted = run(positions, conn, today)

    expired = find_expired([dict(p) for p in positions], today)
    lines, code = summarize_result(len(positions), hits, failed, attempted, expired)
    for line in lines:
        print(line)

    # 到達の可能性・期限切れ・「1件も確認できなかった」のいずれかがあるときだけ
    # 通知する。何も起きていない朝に通知すると、通知そのものが意味を失う。
    #
    # 「1件も確認できなかった」も通知が要る。値動きが1件も取れなかった朝は
    # hits も expired も両方空になり、以前はここで通知しないまま終わっていた。
    # しかし「確認できていない」朝こそ、利用者が自分でSBI（証券会社）を
    # 見にいく必要がある朝であり、通知が届かないと本人はそれに気づけない。
    all_failed = attempted > 0 and failed == attempted
    if hits or expired or all_failed:
        # ロック画面ではこの文面が全文になる。「約定」は初心者には分からない
        # 言葉（注文が成立すること）なので使わない。タイトルだけでも
        # 「何を確認すればいいか」が伝わるようにする。
        parts = []
        if hits:
            parts.append(f"{len(hits)} 件が売れた可能性")
        if expired:
            parts.append(f"{len(expired)} 件が期限切れ")
        if all_failed:
            parts.append("株価が確認できませんでした。自分でSBIを見てください")
        with connect() as conn:
            notify_send(
                conn,
                title="SBIで持ち株を確認してください",
                body=" ／ ".join(parts),
                # 画面のファイル名そのもの。Cloudflare Pages は拡張子なしでも
                # 開けるが、実在するファイル名を指しておくほうが、
                # ホスティングの設定が変わっても壊れない。
                # 通知をタップして開けないと、損切りの知らせが届いても
                # 何も見られないことになる。
                url="/holdings.html",
            )
    return code


if __name__ == "__main__":
    sys.exit(main())
