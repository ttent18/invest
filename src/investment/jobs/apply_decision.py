"""AIが出した判断を検証し、妥当なものだけを proposals に保存する。

AIの出力を信用しない。制約の適用はここで行う。
"""

import json
import sys
from pathlib import Path

from investment.config import BUCKETS, SETTINGS, Settings, bucket_by_name
from investment.db import connect, record_gap
from investment.market import is_japanese, lot_size
from investment.sizing import position_size, required_win_rate

REQUIRED_FIELDS = (
    "symbol", "action", "entry_price", "take_profit", "stop_loss",
    "quantity", "rationale", "scenario", "confidence", "strategy_tag",
)
# 買いのときだけ必須。売りの枠は「その銘柄を買ったときの枠」で決まっているので、
# AIに書かせず保有から取る（書かせると、書き忘れや保有と違う枠を書く事故が起きる）。
BUY_ONLY_REQUIRED_FIELDS = ("bucket",)
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

# 利確・損切りの値が、枠の決めた幅と一致しているとみなす許容誤差。
#
# 例: 899円 × 1.22 = 1096.78円 のように端数が出るため、1円単位に丸めた値も
# 通す必要がある。四捨五入の誤差は最大0.5なので 0.51 あれば必ず吸収できる。
# 一方、株価が高いほど丸め以外のわずかな差も出るため、価格の0.2%も見る。
#
# 固定値（1円）にしないのは、安い株では1円が相対的に大きすぎるため。
# 株価100円だと1円は1%で、損切りが -7%〜-9% のどれでも通ってしまい、
# まさに測ろうとしている数字が12.5%もぶれる。また米国株（ドル建て）が
# 入ったとき、1ドルの誤差は50ドル株で+20%〜+24%を通してしまう。
PRICE_MATCH_MIN_TOLERANCE = 0.51
PRICE_MATCH_TOLERANCE_PCT = 0.002


def _price_tolerance(price: float) -> float:
    """この価格で、丸めによる差とみなす幅を返す。"""
    return max(PRICE_MATCH_MIN_TOLERANCE, abs(price) * PRICE_MATCH_TOLERANCE_PCT)


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
    if decision.get("action") == "buy":
        for field in BUY_ONLY_REQUIRED_FIELDS:
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

        # v3 から、どの枠（じっくり / 回転）で買うかによって利確・損切りの幅が
        # 変わる。枠の指定が無い・知らない名前・幅が枠と違う、のいずれも却下する。
        # ここを通してしまうと「枠」が名前だけのラベルになり、
        # どちらの型が効いているのかを比べられなくなる。
        #
        # **枠の幅は investment.config.BUCKETS（コード）から取る。**
        # context.json は AI が書き換えられる場所にあるため、そこに書かれた幅で
        # 検証すると、AIが自分の数字を自分の数字で検証することになる。
        # 一方、空き枠の数は「そのときの保有状況」なのでコンテキストから取る。
        rule = bucket_by_name(decision["bucket"])
        buckets_by_name = {b["name"]: b for b in ctx.get("buckets", [])}
        bucket = buckets_by_name.get(decision["bucket"])
        if rule is None or bucket is None:
            errors.append(
                f"bucket {decision['bucket']!r} は知らない枠です"
                f"（使えるのは {[b.name for b in BUCKETS]} のいずれか）"
            )
        else:
            if bucket["free"] <= 0:
                errors.append(
                    f"{rule.name}枠に空きがありません"
                    f"（{rule.slots}枠すべて使用中）"
                )
            if entry is not None:
                for field, pct, direction in (
                    ("take_profit", rule.take_profit_pct, 1),
                    ("stop_loss", rule.stop_loss_pct, -1),
                ):
                    expected = entry * (1 + direction * pct)
                    actual = tp if field == "take_profit" else sl
                    if actual is None:
                        continue
                    if abs(actual - expected) > _price_tolerance(expected):
                        errors.append(
                            f"{field} {actual} が{rule.name}枠の決まり"
                            f"（買値の{direction * pct:+.0%}＝{expected:,.2f}円）"
                            f"と違います"
                        )

        if entry is not None and sl is not None and quantity is not None and sl < entry:
            # 日本株は100株単位でしか注文できない（単元）。AIは「上限金額÷株価」で
            # 株数を出しがちだが、その答えは実際には発注できないことがある。
            unit = lot_size(decision["symbol"])
            allowed = position_size(
                settings.total_capital,
                settings.risk_per_trade_pct,
                settings.max_position_pct,
                entry,
                sl,
                lot_size=unit,
            )
            if unit > 1 and quantity % unit != 0:
                errors.append(
                    f"quantity {quantity} は{unit}株単位ではありません"
                    f"（日本株は{unit}株単位でしか注文できません）"
                )
            if allowed == 0:
                # 買えない理由は2つある。どちらなのかを見て言い分けること。
                # まとめて「金額の上限を超える」と言うと、金額は上限内なのに
                # 「上限を超える」と告げる、それ自体で矛盾したメッセージになる。
                cap_amount = settings.total_capital * settings.max_position_pct
                risk_amount = settings.total_capital * settings.risk_per_trade_pct
                if entry * unit > cap_amount:
                    errors.append(
                        f"{decision['symbol']} は{unit}株で {entry * unit:,.0f}円 になり、"
                        f"1銘柄の上限 {cap_amount:,.0f}円 を超えるため買えません"
                    )
                else:
                    errors.append(
                        f"{decision['symbol']} は{unit}株だと損切りまでの損失が "
                        f"{(entry - sl) * unit:,.0f}円 になり、1回の損失上限 "
                        f"{risk_amount:,.0f}円 を超えるため買えません"
                        f"（損切りの幅が広すぎます）"
                    )
            elif quantity > allowed:
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
        else:
            # 売りの枠は、その銘柄を買ったときの枠で決まっている。
            # AIが書いた値があっても、保有側の値で上書きする（保有が正）。
            held_bucket = position.get("bucket")
            if not held_bucket:
                errors.append(
                    f"{decision['symbol']} の保有にどの枠で買ったかの記録がありません"
                    f"（枠が分からないまま保存すると、枠ごとの成績から漏れます）"
                )
            else:
                decision["bucket"] = held_bucket

        if position is not None and quantity is not None:
            held = int(position["quantity"])
            requested = quantity
            if requested > held:
                errors.append(
                    f"{decision['symbol']} の売却株数 {requested} が保有株数 {held} を"
                    f"超えています（保有株数: {held}, 売却しようとした株数: {requested}）"
                )
            # 売りも買いと同じく単元単位でしか注文できない。100株のうち50株だけ
            # 売る、という注文は出せない（v2 で単元未満株は使わないと決めたため）。
            # ただし「保有している分を全部売る」は常に可能。株式分割などで
            # 100株未満の端株を持つことがあり、それを売れないと持ち続けるしか
            # なくなってしまう。
            unit = lot_size(decision["symbol"])
            if unit > 1 and requested != held and requested % unit != 0:
                errors.append(
                    f"売却株数 {requested} は{unit}株単位ではありません"
                    f"（日本株は{unit}株単位でしか注文できません。"
                    f"保有している {held} 株を全部売る場合を除きます）"
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
             required_win_rate, rationale, scenario, confidence, strategy_tag, bucket,
             rule_version, journal_path)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """
    with conn.cursor() as cur:
        cur.executemany(
            sql,
            [
                (
                    d["symbol"], d["action"], d["quantity"], d["entry_price"],
                    d["take_profit"], d["stop_loss"], d["required_win_rate"],
                    d["rationale"], d["scenario"], d["confidence"], d["strategy_tag"],
                    d["bucket"], d["rule_version"], journal_path,
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


def record_no_proposals(conn, ctx: dict, decisions: list[dict]) -> None:
    """分析を実行したのに提案が1件も出なかった事実を data_gaps に記録する。

    これまでは accepted も rejected も0件だと、DBに一切書き込まずに終了して
    いた（呼び出し元で `if accepted or rejected:` と接続ごとスキップしていた）。
    却下された提案は data_gaps に残るのに、「候補が来なかった日」や
    「候補はあったがAIが1件も判断を出さなかった日」は無記録で消えていた。
    このシステムの目的は測定であり、記録が欠けた期間があるとその期間の
    解釈ができなくなるため、ここで記録する。

    「候補が0件だった」のか「候補はあったがAIが全部見送った」のかを区別
    できるよう、候補件数・保有件数・decisions件数をすべて detail に含める。
    """
    detail = (
        f"候補 {len(ctx['candidates'])} 件 / 保有 {len(ctx['positions'])} 件 に対し、"
        f"AIが出した decisions は {len(decisions)} 件でした（採用・却下とも0件）。"
    )
    print(detail)
    record_gap(conn, scope="analysis:no_proposals", detail=detail)


def process(
    conn, ctx: dict, decisions: list[dict], journal_path: str, settings: Settings
) -> tuple[int, int]:
    """decisions を検証し、結果に応じて proposals / data_gaps に記録する。

    戻り値は (採用件数, 却下件数)。
    """
    accepted, rejected = [], []
    for d in decisions:
        errors = validate(d, ctx, settings)
        if errors:
            rejected.append((d.get("symbol", "?"), errors))
        else:
            accepted.append(enrich(d, ctx, settings))

    for symbol, errors in rejected:
        print(f"却下 {symbol}: {'; '.join(errors)}")

    if accepted:
        insert_proposals(conn, accepted, journal_path)
    if rejected:
        record_rejections(conn, rejected)
    if not accepted and not rejected:
        record_no_proposals(conn, ctx, decisions)

    print(f"採用 {len(accepted)} 件 / 却下 {len(rejected)} 件")
    return len(accepted), len(rejected)


def load_decision(path: Path) -> tuple[list[dict], str, tuple[str, str] | None]:
    """AIの判断ファイルを読む。戻り値は (判断のリスト, journalのパス, 読めなかった記録)。

    読めなかった場合に例外を投げないのは、保存の処理を「AIのステップが
    失敗しても必ず実行する」形にしたため。AIが途中で止まればファイルは
    無いか壊れているが、それは異常終了ではなく「今日は判断が出なかった」
    という記録すべき事実である。3番目の戻り値は data_gaps に残すための
    (scope, detail) で、正常に読めた場合は None。
    """
    if not path.exists():
        detail = (
            f"AIの判断ファイル {path} が作られませんでした"
            "（AIのステップが判断を書き終える前に終了した可能性があります）"
        )
        return [], "", ("decision:missing", detail)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        detail = (
            f"AIの判断ファイル {path} が壊れていて読めませんでした: {exc}"
            "（AIが書き終える前に終了した可能性があります）"
        )
        return [], "", ("decision:broken", detail)
    return raw.get("decisions", []), raw.get("journal_path", ""), None


def main() -> int:
    ctx = json.loads(Path("build/context.json").read_text(encoding="utf-8"))
    decisions, journal_path, missing = load_decision(Path("build/decision.json"))

    with connect() as conn:
        if missing is not None:
            record_gap(conn, scope=missing[0], detail=missing[1])
            print(missing[1])
            return 1
        process(conn, ctx, decisions, journal_path, SETTINGS)

    return 0


if __name__ == "__main__":
    sys.exit(main())
