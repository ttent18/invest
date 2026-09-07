"""AIに渡すコンテキストを組み立てる。

AIには判断だけをさせる。計算と制約の適用はここで行い、結果を渡す。
"""

import json
import sys
from pathlib import Path

from investment.config import SCREEN, SETTINGS, ScreenCriteria, Settings
from investment.db import connect, select_cash, select_positions, select_screened

RULE_VERSION = "v1"
OUTPUT = Path("build/context.json")


def build(conn, settings: Settings, criteria: ScreenCriteria, limit: int) -> dict:
    """AIに渡す情報をまとめる。"""
    candidates = select_screened(conn, criteria, limit)
    positions = select_positions(conn)
    cash = select_cash(conn)

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
        "candidates": [dict(c) for c in candidates],
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
