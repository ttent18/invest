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
from investment.market import MarketDataError, fetch_last_price, lot_size
from investment.sizing import position_size

# 適用しているルールの版。rules/<この値>.md が中身。
# 提案・取引にこの値を記録することで、あとから版ごとの成績を比べられる。
# ルールを変えたら rules/vN.md を書いてから、ここを上げること。
RULE_VERSION = "v2"
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


def drop_unaffordable(
    candidates: list[dict], settings: Settings
) -> tuple[list[dict], list[dict]]:
    """1単元すら買えない銘柄を候補から外す。戻り値は (残す候補, 外した候補)。

    日本株は100株単位でしか買えない（単元）。1銘柄に投じられる金額には
    上限があるため、株価が高い銘柄は「100株買うと上限を超える」＝買えない。
    例: 株価2,704円 → 100株で270,400円。上限137,500円を超えるので買えない。

    これをAIに渡しても、発注できない提案しか作れない。実際 2026-09-07 の
    初回運用では、AIが41件の候補のうち5件しか調べきれないまま、
    どちらも発注できない2銘柄を提案した。買えないものは先に外す。
    """
    limit = settings.total_capital * settings.max_position_pct
    kept, dropped = [], []
    for c in candidates:
        unit = lot_size(c["symbol"])
        lot_cost = float(c["last_price"]) * unit
        if lot_cost <= limit:
            kept.append({**c, "lot_size": unit, "max_quantity": _max_quantity(c, settings, unit)})
        else:
            dropped.append({**c, "lot_size": unit, "lot_cost": lot_cost})
    if dropped:
        print(
            f"1単元の金額が1銘柄の上限({limit:,.0f}円)を超えるため "
            f"{len(dropped)} 件を候補から除外しました"
        )
    return kept, dropped


def _max_quantity(candidate: dict, settings: Settings, unit: int) -> int:
    """この銘柄を最大何株まで買えるかを返す（売買単位の倍数）。

    AIが「上限金額 ÷ 株価」を自分で計算すると単元未満の株数を出してしまうため、
    正しい答えをこちらで計算して渡す。
    """
    price = float(candidate["last_price"])
    return position_size(
        settings.total_capital,
        settings.risk_per_trade_pct,
        settings.max_position_pct,
        price,
        price * (1 - settings.stop_loss_pct),
        lot_size=unit,
    )


def assemble(
    candidates: list[dict],
    positions: list[dict],
    cash: dict[str, float],
    settings: Settings,
    criteria: ScreenCriteria,
    dropped: list[dict],
) -> dict:
    """AIに渡す情報をまとめる。ここでは外部アクセスを一切しない。"""
    limit = settings.total_capital * settings.max_position_pct
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
        "excluded_candidates": {
            "reason": (
                f"1単元（日本株は100株）の金額が1銘柄の上限 {limit:,.0f}円 を"
                "超えるため、買えないものとして候補から外した"
            ),
            "count": len(dropped),
            "symbols": [
                {"symbol": d["symbol"], "last_price": d["last_price"],
                 "lot_size": d["lot_size"], "lot_cost": d["lot_cost"]}
                for d in dropped
            ],
        },
    }


def main() -> int:
    # 1. データベースから読む
    with connect() as conn:
        candidates, positions, cash = read_inputs(conn, SCREEN, limit=50)

    # 2. 株価を取る（この間、データベースには接続しない）
    priced_candidates, gaps = attach_last_price(candidates)

    # 2b. 1単元すら買えない銘柄を外す（AIに調べさせても発注できないため）
    priced_candidates, dropped = drop_unaffordable(priced_candidates, SETTINGS)

    # 3. 取れなかった事実を記録する（接続を開き直す）
    if gaps:
        with connect() as conn:
            record_gaps(conn, gaps)

    ctx = assemble(priced_candidates, positions, cash, SETTINGS, SCREEN, dropped)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"候補 {len(ctx['candidates'])} 件 / 保有 {len(ctx['positions'])} 件 → {OUTPUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
