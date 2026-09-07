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
import math
import sys
from pathlib import Path

from investment.config import BUCKETS, MAX_POSITIONS, SCREEN, SETTINGS, ScreenCriteria, Settings
from investment.db import (
    connect,
    record_gap,
    record_gaps,
    select_capital,
    select_cash,
    select_positions,
    select_screened,
)
from investment.market import MarketDataError, fetch_last_price, lot_size
from investment.sizing import position_size

# 適用しているルールの版。rules/<この値>.md が中身。
# 提案・取引にこの値を記録することで、あとから版ごとの成績を比べられる。
# ルールを変えたら rules/vN.md を書いてから、ここを上げること。
RULE_VERSION = "v3"
OUTPUT = Path("build/context.json")


def read_inputs(
    conn, criteria: ScreenCriteria, limit: int
) -> tuple[list[dict], list[dict], dict[str, float], float]:
    """データベースから読むものをまとめて読む。戻り値は (候補, 保有, 現金, 総資金)。

    株価の取得より先にここで読み切ることで、接続を開いている時間を短くする。
    """
    candidates = [dict(c) for c in select_screened(conn, criteria, limit)]
    positions = [dict(p) for p in select_positions(conn)]
    cash = select_cash(conn)
    capital = select_capital(conn)
    return candidates, positions, cash, capital


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


def drop_already_held(
    candidates: list[dict], positions: list[dict]
) -> tuple[list[dict], list[dict]]:
    """既に保有している銘柄を候補から外す。戻り値は (残す候補, 外した候補)。

    保有中の銘柄が翌日また候補に出ると、AIはそれを「買い増し」として
    提案できてしまう。買い増すと、investment.fills.apply_buy が平均取得
    単価を計算し直し、そこから利確・損切りの価格を引き直す。しかし
    SBIに実際に置いてある逆指値（この値段まで下がったら売る、という
    予約注文）は古い値のままで、誰も「置き直してください」と言わない。
    以後 morning_check は実在しない注文の価格で「売れた可能性」を
    判定し続けることになる。

    そもそも候補に出さなければ、AIがその提案をする余地自体が無くなる
    （apply_decision.validate() でも同じ内容を独立に確かめており、
    ここは一段目の守りにあたる）。
    """
    held = {p["symbol"] for p in positions}
    kept, dropped = [], []
    for c in candidates:
        if c["symbol"] in held:
            dropped.append(c)
        else:
            kept.append(c)
    if dropped:
        print(f"既に保有している銘柄のため {len(dropped)} 件を候補から除外しました")
    return kept, dropped


def drop_unaffordable(
    candidates: list[dict], settings: Settings, capital: float
) -> tuple[list[dict], list[dict]]:
    """1単元すら買えない銘柄を候補から外す。戻り値は (残す候補, 外した候補)。

    日本株は100株単位でしか買えない（単元）。1銘柄に投じられる金額には
    上限があるため、株価が高い銘柄は「100株買うと上限を超える」＝買えない。
    例: 株価2,704円 → 100株で270,400円。上限137,500円を超えるので買えない。

    これをAIに渡しても、発注できない提案しか作れない。実際 2026-09-07 の
    初回運用では、AIが41件の候補のうち5件しか調べきれないまま、
    どちらも発注できない2銘柄を提案した。買えないものは先に外す。
    """
    # 注意: この比較は株価が円建てであることを前提にしている。
    # 米国株（ドル建て）を候補に入れる場合は、ここで為替換算が必要になる。
    # 現在スクリーニングの対象は日本株のみなので、まだ問題は起きていない。
    limit = capital * settings.max_position_pct
    kept, dropped = [], []
    for c in candidates:
        unit = lot_size(c["symbol"])
        lot_cost = float(c["last_price"]) * unit
        if lot_cost <= limit:
            quantity = _max_quantity(c, settings, unit, capital)
            kept.append({
                **c,
                "lot_size": unit,
                "max_quantity": quantity,
                "max_entry_price": _max_entry_price(quantity, limit),
            })
        else:
            dropped.append({**c, "lot_size": unit, "lot_cost": lot_cost})
    if dropped:
        print(
            f"1単元の金額が1銘柄の上限({limit:,.0f}円)を超えるため "
            f"{len(dropped)} 件を候補から除外しました"
        )
    return kept, dropped


def _max_quantity(candidate: dict, settings: Settings, unit: int, capital: float) -> int:
    """この銘柄を最大何株まで買えるかを返す（売買単位の倍数）。

    AIが「上限金額 ÷ 株価」を自分で計算すると単元未満の株数を出してしまうため、
    正しい答えをこちらで計算して渡す。

    枠ごとに損切り幅が違うので、いちばん広い損切り幅で計算する。損切りが広い
    ほど1株あたりの想定損失が大きくなり、買える株数は少なくなるため、
    これがどの枠でも通る安全側の値になる。
    （実際には損切り8%以内なら金額の上限のほうが先に効くので、
    今の2つの枠では同じ値になる。config の test がこの前提を見張っている。）
    """
    price = float(candidate["last_price"])
    widest_stop = max(b.stop_loss_pct for b in BUCKETS)
    return position_size(
        capital,
        settings.risk_per_trade_pct,
        settings.max_position_pct,
        price,
        price * (1 - widest_stop),
        lot_size=unit,
    )


def _max_entry_price(max_quantity: int, limit: float) -> float:
    """max_quantity 株を保ったまま出せる買値の上限を返す。

    AIは買値を last_price の ±10% の範囲で決めてよいことになっている。
    しかし max_quantity は last_price を基準に計算した株数なので、
    買値を上げると同じ株数では金額の上限を超えてしまう。日本株は100株単位で
    「1株だけ減らす」ができないため、指示どおりに答えた提案がまるごと
    却下されることになる（2026-09-07 のレビューで判明）。

    そこで「その株数のまま出せる買値の上限」を計算して渡し、
    買値を上げるなら株数を1単元減らす必要があることを分かるようにする。
    切り上げると上限を超えてしまうため、1円未満は切り捨てる。
    """
    if max_quantity <= 0:
        return 0.0
    return math.floor(limit / max_quantity)


def _slots_used(positions: list[dict]) -> dict[str, int]:
    """枠ごとに、いま何銘柄を持っているかを数える。

    保有に枠の記録が無い場合や、知らない枠の名前だった場合は
    「じっくり枠」として数える（計画1では保有を記録していないため、当面は
    起こらない）。黙って0にすると、枠が空いていないのに空いていると
    伝えてしまうため。
    """
    known = {b.name for b in BUCKETS}
    used: dict[str, int] = {}
    for p in positions:
        name = p.get("bucket")
        # 記録が無い場合も、知らない名前だった場合も、最初の枠に数える。
        # 知らない名前のまま数えると assemble 側が読まないキーに入り、
        # 「数えたつもりで数え落とす」という、いちばん避けたい形になる。
        if name not in known:
            name = BUCKETS[0].name
        used[name] = used.get(name, 0) + 1
    return used


def assemble(
    candidates: list[dict],
    positions: list[dict],
    cash: dict[str, float],
    settings: Settings,
    criteria: ScreenCriteria,
    dropped: list[dict],
    capital: float,
    held_dropped: list[dict] | None = None,
) -> dict:
    """AIに渡す情報をまとめる。ここでは外部アクセスを一切しない。"""
    limit = capital * settings.max_position_pct
    used = _slots_used(positions)
    # 枠ごとの空きを足すと、全体の残り枠を超えることがある。
    # 例: 3銘柄すべて回転枠 → 回転0・じっくり2 で合計2だが、全体の残りは1。
    # AIに矛盾した数を見せないよう、全体の残りで頭打ちにする。
    #
    # **これは表示を正すだけで、上限を守らせているのはここではない。**
    # 実際に「1回の判断で枠の数を超えて買う」のを止めているのは
    # apply_decision.process()（採用した件数を数えながら検証する）である。
    remaining = max(0, MAX_POSITIONS - len(positions))
    return {
        "is_virtual": True,
        "rule_version": RULE_VERSION,
        "constraints": {
            "total_capital": capital,
            "max_position_pct": settings.max_position_pct,
            "max_positions": MAX_POSITIONS,
            "risk_per_trade_pct": settings.risk_per_trade_pct,
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
        "can_open_new": len(positions) < MAX_POSITIONS,
        "buckets": [
            {
                "name": b.name,
                "take_profit_pct": b.take_profit_pct,
                "stop_loss_pct": b.stop_loss_pct,
                "max_holding_days": b.max_holding_days,
                "slots": b.slots,
                "used": used.get(b.name, 0),
                "free": min(remaining, max(0, b.slots - used.get(b.name, 0))),
            }
            for b in BUCKETS
        ],
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
        # 買い増しの提案自体をさせないため、既に保有している銘柄は候補に
        # 出す前に外している（drop_already_held）。外した事実を黙って
        # 捨てず、上の excluded_candidates と同じ扱いで残す。
        "excluded_held_candidates": {
            "reason": (
                "既に保有している銘柄です。買い増しを提案させると、SBIに"
                "置いてある逆指値（この値段まで下がったら売る、という"
                "予約注文）が古い値のままになり、実際の損切り価格とずれる"
                "ため、候補から外した"
            ),
            "count": len(held_dropped or []),
            "symbols": [d["symbol"] for d in (held_dropped or [])],
        },
    }


def main() -> int:
    # 1. データベースから読む
    with connect() as conn:
        candidates, positions, cash, capital = read_inputs(conn, SCREEN, limit=50)

        # 総資金が0円（以下）のときは、ここで止めて記録する。cash に JPY の
        # 行が無い・消えた場合、select_capital は黙って0.0を返す。それに
        # 気づかず進むと、1銘柄の上限が0円になって候補が全部除外されるが、
        # 見た目は「今日は買える銘柄が無かった」正常終了と区別できない。
        if capital <= 0:
            detail = (
                "総資金が0円です。現金の記録が入っていない可能性があります"
                "（investment.jobs.init_portfolio がまだ実行されていない、"
                "または cash の記録が消えたなど）"
            )
            record_gap(conn, scope="capital:zero", detail=detail)
            print(detail)
            return 1

    # 1b. 既に保有している銘柄を候補から外す（AIに買い増しを提案させない）
    candidates, held_dropped = drop_already_held(candidates, positions)

    # 2. 株価を取る（この間、データベースには接続しない）
    priced_candidates, gaps = attach_last_price(candidates)

    # 2b. 1単元すら買えない銘柄を外す（AIに調べさせても発注できないため）
    priced_candidates, dropped = drop_unaffordable(priced_candidates, SETTINGS, capital)

    # 3. 外した事実を記録する（接続を開き直す）。
    #    context.json は git 管理外で毎回上書きされ、実行ログも消えるため、
    #    「何が候補から外れたのか」を後から数えられるのはここだけになる。
    cap = capital * SETTINGS.max_position_pct
    gaps = gaps + [
        (
            f"candidate_unaffordable:{d['symbol']}",
            (
                f"{d['symbol']} は{d['lot_size']}株で {d['lot_cost']:,.0f}円 になり、"
                f"1銘柄の上限 {cap:,.0f}円 を超えるため候補から外しました"
            ),
        )
        for d in dropped
    ] + [
        (
            f"candidate_already_held:{d['symbol']}",
            f"{d['symbol']} は既に保有しているため、買い増しの提案を防ぐために候補から外しました",
        )
        for d in held_dropped
    ]
    if gaps:
        with connect() as conn:
            record_gaps(conn, gaps)

    ctx = assemble(
        priced_candidates, positions, cash, SETTINGS, SCREEN, dropped, capital,
        held_dropped=held_dropped,
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(ctx, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"候補 {len(ctx['candidates'])} 件 / 保有 {len(ctx['positions'])} 件 / "
        f"総資金 {capital:,.0f}円 → {OUTPUT}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
