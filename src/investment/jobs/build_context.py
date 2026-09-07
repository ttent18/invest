"""AIに渡すコンテキストを組み立てる。

AIには判断だけをさせる。計算と制約の適用はここで行い、結果を渡す。
"""

import json
import sys
from pathlib import Path

from investment.config import SCREEN, SETTINGS, ScreenCriteria, Settings
from investment.db import connect, record_gap, select_cash, select_positions, select_screened
from investment.market import MarketDataError, fetch_last_price

RULE_VERSION = "v1"
OUTPUT = Path("build/context.json")


def _attach_last_price(conn, candidates: list[dict]) -> list[dict]:
    """各候補に直近の株価 (last_price) を付与する。

    株価は Task 10 の AI が entry_price / take_profit / stop_loss / quantity を
    答えるために必須。取得できない銘柄で判断させると数字をでっち上げることに
    なり、Task 9 の検証（株数上限・必要勝率の計算）も無意味になるため、
    株価が取れなかった銘柄は候補から除外する（古い価格は使わない）。
    除外した事実は record_gap に記録し、標準出力にも出す。
    """
    priced = []
    excluded = 0
    for c in candidates:
        symbol = c["symbol"]
        try:
            last_price = fetch_last_price(symbol)
        except MarketDataError as exc:
            excluded += 1
            record_gap(conn, scope=f"price:{symbol}", detail=str(exc))
            continue
        priced.append({**c, "last_price": last_price})

    if excluded:
        print(f"株価が取れなかったため {excluded} 件を候補から除外しました")

    return priced


def build(conn, settings: Settings, criteria: ScreenCriteria, limit: int) -> dict:
    """AIに渡す情報をまとめる。"""
    candidates = select_screened(conn, criteria, limit)
    positions = select_positions(conn)
    cash = select_cash(conn)
    priced_candidates = _attach_last_price(conn, [dict(c) for c in candidates])

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
        "positions": [dict(p) for p in positions],
        "can_open_new": len(positions) < settings.max_positions,
        "candidates": priced_candidates,
    }


def main() -> int:
    with connect() as conn:
        ctx = build(conn, SETTINGS, SCREEN, limit=50)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"候補 {len(ctx['candidates'])} 件 / 保有 {len(ctx['positions'])} 件 → {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
