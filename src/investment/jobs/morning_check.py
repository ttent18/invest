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

from investment.db import connect, record_gap, select_positions
from investment.market import MarketDataError, fetch_range

JST = ZoneInfo("Asia/Tokyo")  # 「今日」は日本時間で決める（GitHub Actions は UTC で動くため）

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
    """
    ranges: dict[str, tuple[float, float]] = {}
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
            record_gap(conn, scope=f"daily_range:{p['symbol']}", detail=str(exc))

    hits = detect_hits(positions, ranges)
    return hits, failed, attempted


def summarize_result(
    positions_count: int, hits: list[dict], failed: int, attempted: int
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

    lines, code = summarize_result(len(positions), hits, failed, attempted)
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
