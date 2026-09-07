"""毎朝、前日の値動きから約定した可能性のある銘柄を洗い出す。

SBI証券には参照系のAPIがなく、米国株は約定通知メールも来ない。
そのため前日の高値・安値と、SBIに置いた利確・損切りの値を突き合わせて推定する。
"""

import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from investment.db import connect, record_gap, select_positions
from investment.market import MarketDataError, fetch_daily_range

JST = ZoneInfo("Asia/Tokyo")  # 「前日」は日本時間で決める（GitHub Actions は UTC で動くため）


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


def main() -> int:
    yesterday = datetime.now(tz=JST).date() - timedelta(days=1)
    with connect() as conn:
        positions = select_positions(conn)
        if not positions:
            print("保有なし。確認は不要です")
            return 0

        ranges: dict[str, tuple[float, float]] = {}
        for p in positions:
            try:
                ranges[p["symbol"]] = fetch_daily_range(p["symbol"], yesterday)
            except MarketDataError as exc:
                record_gap(conn, scope=f"daily_range:{p['symbol']}", detail=str(exc))

        hits = detect_hits(positions, ranges)

    if not hits:
        print(f"保有 {len(positions)} 件。売買はありませんでした。確認は不要です")
        return 0

    print(f"次の {len(hits)} 件は約定した可能性があります。SBIで確認してください:")
    for h in hits:
        label = "損切り" if h["kind"] == "stop_loss" else "利確"
        print(f"  {h['symbol']}  {label} {h['estimated_price']} に到達"
              f"（前日 高値 {h['day_high']} / 安値 {h['day_low']}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
