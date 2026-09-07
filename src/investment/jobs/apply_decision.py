"""AIが出した判断を検証し、妥当なものだけを proposals に保存する。

AIの出力を信用しない。制約の適用はここで行う。
"""

import json
import sys
from pathlib import Path

from investment.config import SETTINGS, Settings
from investment.db import connect, record_gap
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


def _to_float(decision: dict, field: str) -> tuple[float | None, str | None]:
    """decision[field] を float に変換する。失敗したら (None, エラーメッセージ)。

    AIの出力はJSONなので、null(→None)や、桁を勘違いした文字列
    (例: "2450円")が混ざりうる。これまでは float() を無条件に呼んでおり、
    TypeError / ValueError が validate() の外へ漏れて main() ごと失敗させ、
    同じバッチの正当な提案や journal のコミットまで道連れにしていた。
    ここで例外を吸収し、却下メッセージに変換する。
    """
    value = decision[field]
    try:
        return float(value), None
    except (TypeError, ValueError):
        return None, f"{field} が数値として解釈できません（値: {value!r}）"


def _to_positive_int_quantity(decision: dict) -> tuple[int | None, str | None]:
    """decision["quantity"] を1以上の整数に変換する。失敗したら (None, エラーメッセージ)。

    quantity=0 や負の値は買い・売りどちらの既存チェックも素通りしていた
    (買いは上限チェックのみ、売りは保有超過チェックのみだったため)。
    また、33.9のような端数を int() で暗黙に切り捨てて33として検証すると、
    検証した値(33)と decision に残ったままの値(33.9)が食い違い、
    後段の insert_proposals には生の値(33.9)が渡ってしまう。
    そのため端数がある場合はここで却下し、整数のときだけ int を返す。
    """
    value = decision["quantity"]
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None, f"quantity が数値として解釈できません（値: {value!r}）"
    if f != int(f):
        return None, f"quantity は整数である必要があります（値: {value!r}）"
    n = int(f)
    if n < 1:
        return None, f"quantity は1以上である必要があります（値: {value!r}）"
    return n, None


def validate(decision: dict, ctx: dict, settings: Settings) -> list[str]:
    """判断が制約を満たすか調べる。問題があればメッセージを返す。

    検証に通った場合、decision["quantity"] は検証で使った整数値に
    正規化される(例: 33.0 → 33)。insert_proposals はこの decision を
    そのまま使うため、検証した値と実際に保存される値を一致させるための
    副作用として行っている。
    """
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

    entry, err = _to_float(decision, "entry_price")
    if err:
        errors.append(err)
    tp, err = _to_float(decision, "take_profit")
    if err:
        errors.append(err)
    sl, err = _to_float(decision, "stop_loss")
    if err:
        errors.append(err)
    quantity, err = _to_positive_int_quantity(decision)
    if err:
        errors.append(err)
    else:
        decision["quantity"] = quantity

    if entry is not None and tp is not None and sl is not None and not (sl < entry < tp):
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
        elif entry is not None:
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

        if entry is not None and sl is not None and quantity is not None and sl < entry:
            allowed = position_size(
                settings.total_capital,
                settings.risk_per_trade_pct,
                settings.max_position_pct,
                entry,
                sl,
            )
            if quantity > allowed:
                errors.append(f"quantity {quantity} が上限 {allowed} 株を超えています")

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
        elif quantity is not None:
            held = int(position["quantity"])
            requested = quantity
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


def record_rejections(conn, rejected: list[tuple[str, list[str]]]) -> None:
    """却下された提案を data_gaps に記録する。proposals には入れない。

    却下された提案は quantity<=0 のように proposals の CHECK 制約に
    違反しうる値を持つことがあるため、そのまま insert すると失敗し、
    バッチ全体(正当な提案や journal のコミットも含む)を道連れにしてしまう。
    data_gaps には制約が無く、scope・detail に自由なテキストを持てるため、
    ここに却下理由の全文を残す。「AIが何を間違えるか」を測るうえで、
    却下された判断こそ最も価値の高い信号であり、記録から漏らしてはならない。
    """
    for symbol, errors in rejected:
        record_gap(conn, scope=f"proposal_rejected:{symbol}", detail="; ".join(errors))


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

    if accepted or rejected:
        with connect() as conn:
            if accepted:
                insert_proposals(conn, accepted, journal_path)
            if rejected:
                record_rejections(conn, rejected)

    print(f"採用 {len(accepted)} 件 / 却下 {len(rejected)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
