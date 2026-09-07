"""AIに渡すコンテキストを組み立てる。

AIには判断だけをさせる。計算と制約の適用はここで行い、結果を渡す。

## データベースを読む → 株価を取る → データベースに書く、の順で分けている理由

Neon(利用しているデータベース)は、数分間アクセスが無いと接続を自動的に
切断する(無料枠の省電力機能)。株価の取得は1銘柄あたり1秒近くかかるため、
接続を開いたまま候補50件ぶんの株価を取りに行くと、その間ずっと接続が
放置される。今の件数ならまだ切られないが、候補数を増やせば切られる。

そのため、

  1. データベースから読むものを先に全部読む(接続を閉じる)
  2. 株価を取る(この間、データベースには一切触れない)
  3. 必要なら接続を開き直して書く

という順序にしてある。同じ理由で run_screen.py も同じ構造になっている。
"""

import json
import sys
from pathlib import Path

from investment.config import SCREEN, SETTINGS, ScreenCriteria, Settings
from investment.db import connect, record_gaps, select_cash, select_positions, select_screened
from investment.market import MarketDataError, fetch_last_price

RULE_VERSION = "v1"
OUTPUT = Path("build/context.json")


def read_inputs(
    conn, criteria: ScreenCriteria, limit: int
) -> tuple[list[dict], list[dict], dict[str, float]]:
    """データベースから読むものをまとめて読む。戻り値は (候補, 保有, 現金)。

    株価の取得より先にここで読み切ることで、接続を開いている時間を短くする。
    """
    candidates = [dict(c) for c in select_screened(conn, criteria, limit)]
    positions = [dict(p) for p in select_positions(conn)]
    cash = select_cash(conn)
    return candidates, positions, cash


def attach_last_price(candidates: list[dict]) -> tuple[list[dict], list[tuple[str, str]]]:
    """各候補に直近の株価 (last_price) を付与する。データベースには触れない。

    株価は AI が entry_price / take_profit / stop_loss / quantity を答えるために
    必須。取得できない銘柄で判断させると数字をでっち上げることになり、
    発注前の検証（株数上限・必要勝率の計算）も無意味になるため、
    株価が取れなかった銘柄は候補から除外する（古い価格は使わない）。

    戻り値は (株価が付いた候補, 取れなかった事実の記録)。
    記録は呼び出し側がまとめて data_gaps に書き込む。
    """
    priced = []
    gaps: list[tuple[str, str]] = []
    for c in candidates:
        symbol = c["symbol"]
        try:
            last_price = fetch_last_price(symbol)
        except MarketDataError as exc:
            gaps.append((f"price:{symbol}", str(exc)))
            continue
        priced.append({**c, "last_price": last_price})

    if gaps:
        print(f"株価が取れなかったため {len(gaps)} 件を候補から除外しました")

    return priced, gaps


def assemble(
    candidates: list[dict],
    positions: list[dict],
    cash: dict[str, float],
    settings: Settings,
    criteria: ScreenCriteria,
) -> dict:
    """AIに渡す情報をまとめる。ここでは外部アクセスを一切しない。"""
    return {
        "is_virtual": True,
        "rule_version": RULE_VERSION,
        "constraints": {
            "total_capital": settings.total_capital,
            "max_position_pct": settings.max_position_pct,
            "max_positions": settings.max_positions,
            "risk_per_trade_pct": settings.risk_per_trade_pct,
            "stop_loss_pct": settings.stop_loss_pct,
            "take_profit_pct": settings.take_profit_pct,
            "jp_fee_rate": settings.jp_fee_rate,
            "us_fee_rate": settings.us_fee_rate,
        },
        "screen_criteria": {
            "max_market_cap": criteria.max_market_cap,
            "min_revenue_growth": criteria.min_revenue_growth,
            "min_operating_margin": criteria.min_operating_margin,
            "min_roe": criteria.min_roe,
            "min_equity_ratio": criteria.min_equity_ratio,
        },
        "cash": cash,
        "positions": positions,
        "can_open_new": len(positions) < settings.max_positions,
        "candidates": candidates,
    }


def main() -> int:
    # 1. データベースから読む
    with connect() as conn:
        candidates, positions, cash = read_inputs(conn, SCREEN, limit=50)

    # 2. 株価を取る（この間、データベースには接続しない）
    priced_candidates, gaps = attach_last_price(candidates)

    # 3. 取れなかった事実を記録する（接続を開き直す）
    if gaps:
        with connect() as conn:
            record_gaps(conn, gaps)

    ctx = assemble(priced_candidates, positions, cash, SETTINGS, SCREEN)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"候補 {len(ctx['candidates'])} 件 / 保有 {len(ctx['positions'])} 件 → {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
