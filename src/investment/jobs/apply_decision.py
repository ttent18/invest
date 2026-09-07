"""AIが出した判断を検証し、妥当なものだけを proposals に保存する。

AIの出力を信用しない。制約の適用はここで行う。
"""

import json
import sys
from pathlib import Path

from investment.config import SETTINGS, Settings
from investment.db import connect
from investment.market import is_japanese
from investment.sizing import position_size, required_win_rate

REQUIRED_FIELDS = (
    "symbol", "action", "entry_price", "take_profit", "stop_loss",
    "quantity", "rationale", "scenario", "confidence", "strategy_tag",
)
VALID_CONFIDENCE = {"low", "mid", "high"}
VALID_ACTION = {"buy", "sell"}

# entry_price(AIが自己申告した買値)は、そのままでは信用できない。
# AIは銘柄を勘違いしたり、桁を間違えたりすることがあるため、実際の市場価格
# (candidates の last_price)と比べて大きくズレていないかをここで確認する。
# 許容幅を設けているのは、株価は取引時間中ずっと動いているため、コンテキスト
# 生成時点の last_price と多少ズレるのは正常な誤差だから。
# 10%は「通常の値動きは許容しつつ、明らかな間違い(例: 桁違い)は弾く」ための
# 目安として選んだ値。
ENTRY_PRICE_TOLERANCE = 0.10


def validate(decision: dict, ctx: dict, settings: Settings) -> list[str]:
    """判断が制約を満たすか調べる。問題があればメッセージを返す。"""
    errors: list[str] = []

    for field in REQUIRED_FIELDS:
        if field not in decision:
            errors.append(f"{field} がありません")
    if errors:
        return errors

    if decision["action"] not in VALID_ACTION:
        errors.append(f"action は {VALID_ACTION} のいずれかである必要があります")
    if decision["confidence"] not in VALID_CONFIDENCE:
        errors.append(f"confidence は {VALID_CONFIDENCE} のいずれかである必要があります")
    if not str(decision["rationale"]).strip():
        errors.append("rationale が空です")
    if not str(decision["scenario"]).strip():
        errors.append("scenario が空です")

    entry = float(decision["entry_price"])
    tp = float(decision["take_profit"])
    sl = float(decision["stop_loss"])
    if not (sl < entry < tp):
        errors.append("stop_loss < entry_price < take_profit である必要があります")

    if decision["action"] == "buy":
        candidates_by_symbol = {c["symbol"]: c for c in ctx["candidates"]}
        candidate = candidates_by_symbol.get(decision["symbol"])

        if candidate is None:
            errors.append(f"{decision['symbol']} はスクリーニングの候補に含まれていません")
        elif "last_price" not in candidate:
            errors.append(
                f"{decision['symbol']} の候補データに last_price (実際の株価) がありません"
            )
        else:
            last_price = float(candidate["last_price"])
            lower = last_price * (1 - ENTRY_PRICE_TOLERANCE)
            upper = last_price * (1 + ENTRY_PRICE_TOLERANCE)
            if not (lower <= entry <= upper):
                errors.append(
                    f"entry_price {entry} が実際の株価 {last_price} から "
                    f"±{ENTRY_PRICE_TOLERANCE:.0%} を超えて離れています"
                    f"（実際の株価: {last_price}, 提案された買値: {entry}）"
                )

        if not ctx["can_open_new"]:
            errors.append("同時保有の上限に達しているため新規に買えません")

        if sl < entry:
            allowed = position_size(
                settings.total_capital,
                settings.risk_per_trade_pct,
                settings.max_position_pct,
                entry,
                sl,
            )
            if int(decision["quantity"]) > allowed:
                errors.append(
                    f"quantity {decision['quantity']} が上限 {allowed} 株を超えています"
                )

    elif decision["action"] == "sell":
        # 買いだけ疑って売りを信用する理由はない。AIが保有していない銘柄を
        # 売ったり、保有株数を超える株数を売ったりしていないかをここで確認する。
        # 必要なデータ(現在の保有)は ctx["positions"] に既にある。
        #
        # 保有銘柄の現在値がコンテキストに無いため、entry_price の±10%チェック
        # (買いで行っているもの)は売りには実装しない。別途取得が必要になり
        # 範囲が広がるため、ここでは見送る。
        positions_by_symbol = {p["symbol"]: p for p in ctx["positions"]}
        position = positions_by_symbol.get(decision["symbol"])

        if position is None:
            errors.append(f"{decision['symbol']} を保有していないため売却できません")
        else:
            held = int(position["quantity"])
            requested = int(decision["quantity"])
            if requested > held:
                errors.append(
                    f"{decision['symbol']} の売却株数 {requested} が保有株数 {held} を"
                    f"超えています（保有株数: {held}, 売却しようとした株数: {requested}）"
                )

    return errors


def enrich(decision: dict, ctx: dict, settings: Settings) -> dict:
    """必要勝率と rule_version を付け加える。

    rule_version は ctx（build_context.py が生成したコンテキスト。実際に
    使われたルールのバージョンを持つ）から取る。AIが出した decision の
    中身は信用しないという本モジュールの方針上、rule_version もAIの出力
    ではなく ctx の値をそのまま使う（AIがこのフィールドを書いてきても
    上書きする）。

    ctx に rule_version が無い場合、"v1" 等に静かにフォールバックすると、
    本当のバージョンが分からないまま proposals に記録されてしまい、
    設計書16章が求める「バージョン別の成績比較」が気づかれないまま壊れる。
    そのため、無い場合はここで例外にして早期に気づけるようにする。
    """
    fee = settings.jp_fee_rate if is_japanese(decision["symbol"]) else settings.us_fee_rate
    decision["required_win_rate"] = required_win_rate(
        float(decision["entry_price"]),
        float(decision["take_profit"]),
        float(decision["stop_loss"]),
        fee,
    )
    if "rule_version" not in ctx:
        raise KeyError("ctx に rule_version がありません。rule_version を記録できません。")
    decision["rule_version"] = ctx["rule_version"]
    return decision


def insert_proposals(conn, decisions: list[dict], journal_path: str) -> int:
    """検証済みの判断を保存する。

    各 decision は事前に enrich() 済みで、"rule_version"（AIの出力ではなく
    ctx 由来の実際のルールバージョン）を持っている前提。ここで "v1" 等に
    フォールバックしてしまうと、enrich() が rule_version を正しく設定して
    いてもその値を隠してしまうため、フォールバックは行わず decision の
    値をそのまま使う。
    """
    sql = """
        INSERT INTO proposals
            (created_at, symbol, action, quantity, entry_price, take_profit, stop_loss,
             required_win_rate, rationale, scenario, confidence, strategy_tag,
             rule_version, journal_path)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    d["symbol"], d["action"], d["quantity"], d["entry_price"],
                    d["take_profit"], d["stop_loss"], d["required_win_rate"],
                    d["rationale"], d["scenario"], d["confidence"], d["strategy_tag"],
                    d["rule_version"], journal_path,
                )
                for d in decisions
            ],
        )
    conn.commit()
    return len(decisions)


def main() -> int:
    ctx = json.loads(Path("build/context.json").read_text(encoding="utf-8"))
    raw = json.loads(Path("build/decision.json").read_text(encoding="utf-8"))
    decisions = raw.get("decisions", [])
    journal_path = raw.get("journal_path", "")

    accepted, rejected = [], []
    for d in decisions:
        errors = validate(d, ctx, SETTINGS)
        if errors:
            rejected.append((d.get("symbol", "?"), errors))
        else:
            accepted.append(enrich(d, ctx, SETTINGS))

    for symbol, errors in rejected:
        print(f"却下 {symbol}: {'; '.join(errors)}")

    if accepted:
        with connect() as conn:
            insert_proposals(conn, accepted, journal_path)

    print(f"採用 {len(accepted)} 件 / 却下 {len(rejected)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
