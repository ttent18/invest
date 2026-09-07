import json
import os
from unittest.mock import patch

import pytest

from investment.config import SETTINGS
from investment.db import apply_migrations, connect
from investment.jobs.apply_decision import enrich, insert_proposals, main, process, validate

CTX = {
    "can_open_new": True,
    # last_price は good() の entry_price(1200.0) と整合させてある。
    # ズレのテストは各テストの中で candidates を差し替えて行う。
    #
    # 日本株は100株単位でしか買えない。1銘柄上限は 550,000円 × 25% = 137,500円
    # なので、100株で収まる株価（1,375円以下）を基準の銘柄にしてある。
    "candidates": [{"symbol": "3993.T", "last_price": 1200.0}],
    "rule_version": "v2",
    # 保有無し。売りの検証テストは各テストの中で positions を差し替えて行う。
    "positions": [],
    "buckets": [
        {"name": "じっくり", "take_profit_pct": 0.22, "stop_loss_pct": 0.08,
         "max_holding_days": None, "slots": 2, "used": 0, "free": 2},
        {"name": "回転", "take_profit_pct": 0.10, "stop_loss_pct": 0.05,
         "max_holding_days": 10, "slots": 2, "used": 0, "free": 2},
    ],
}


def good(**overrides) -> dict:
    d = {
        "symbol": "3993.T",
        "action": "buy",
        "entry_price": 1200.0,
        "take_profit": 1464.0,   # +22%
        "stop_loss": 1104.0,     # -8%
        "quantity": 100,         # 日本株の売買単位は100株
        "rationale": "決算で売上が伸びている",
        "scenario": "2〜3週間で調整前の水準に戻る想定",
        "confidence": "mid",
        "strategy_tag": "短期モメンタム",
        "bucket": "じっくり",
    }
    d.update(overrides)
    return d


def test_valid_decision_has_no_errors():
    assert validate(good(), CTX, SETTINGS, capital=550_000.0) == []


def test_rejects_missing_stop_loss():
    d = good()
    del d["stop_loss"]
    errors = validate(d, CTX, SETTINGS, capital=550_000.0)
    assert any("stop_loss" in e for e in errors)


def test_rejects_symbol_not_in_candidates():
    errors = validate(good(symbol="7203.T"), CTX, SETTINGS, capital=550_000.0)
    assert any("候補に含まれていません" in e for e in errors)


def test_rejects_quantity_over_position_limit():
    # 上限25% = 137,500円 → 1200円なら114株買えるが、100株単位なので上限は100株。
    # 200株は上限を超える
    errors = validate(good(quantity=200), CTX, SETTINGS, capital=550_000.0)
    assert any("上限" in e for e in errors)


def test_rejects_when_no_room_for_new_position():
    ctx = dict(CTX, can_open_new=False)
    errors = validate(good(), ctx, SETTINGS, capital=550_000.0)
    assert any("同時保有" in e for e in errors)


def test_rejects_stop_loss_above_entry():
    errors = validate(good(stop_loss=2500.0), CTX, SETTINGS, capital=550_000.0)
    assert any("stop_loss" in e for e in errors)


def test_rejects_unknown_confidence():
    errors = validate(good(confidence="とても高い"), CTX, SETTINGS, capital=550_000.0)
    assert any("confidence" in e for e in errors)


def test_rejects_empty_rationale():
    errors = validate(good(rationale="  "), CTX, SETTINGS, capital=550_000.0)
    assert any("rationale" in e for e in errors)


# --- entry_price と実際の株価(last_price)のズレを検証するテスト ---
# AIは entry_price を自己申告するだけで、それが実際の株価と近いかは保証されない。
# last_price(市場から取得した実際の価格)と±10%を超えて離れていたら却下する。


def test_accepts_entry_price_matching_last_price():
    # entry_price(1200.0) は candidates の last_price(1200.0) と一致
    assert validate(good(), CTX, SETTINGS, capital=550_000.0) == []


def test_accepts_entry_price_at_plus_10_percent_boundary():
    last_price = 1200.0
    entry = last_price * 1.10  # 1320.0 ちょうど。境界は許容する
    # sl/tp も entry からの -8%/+22% に合わせて計算し直す(順序制約を壊さないため)
    d = good(
        entry_price=entry,
        take_profit=entry * 1.22,
        stop_loss=entry * 0.92,
        # 上限137,500円 → 1320円で104株。100株単位なので100株が上限
        quantity=100,
    )
    assert validate(d, CTX, SETTINGS, capital=550_000.0) == []
    # 次に出せる株数(200株)は上限を超えるので却下される
    assert validate(dict(d, quantity=200), CTX, SETTINGS, capital=550_000.0) != []


def test_accepts_entry_price_at_minus_10_percent_boundary():
    last_price = 1200.0
    entry = last_price * 0.90  # 1080.0 ちょうど。境界は許容する
    d = good(
        entry_price=entry,
        take_profit=entry * 1.22,
        stop_loss=entry * 0.92,
        # 上限137,500円 → 1080円で127株。100株単位なので100株が上限
        quantity=100,
    )
    assert validate(d, CTX, SETTINGS, capital=550_000.0) == []


def test_rejects_entry_price_far_from_last_price():
    # 実際の株価は1200円なのに、買値100円は明らかにおかしい(捏造や勘違いの疑い)
    errors = validate(good(entry_price=100.0), CTX, SETTINGS, capital=550_000.0)
    assert any("1200" in e and "100" in e for e in errors)


def test_rejects_when_last_price_missing_from_candidate():
    # last_price キーが無い候補は異常。あるはずのものが無いので却下する
    ctx = {
        "can_open_new": True,
        "candidates": [{"symbol": "3993.T"}],
        "rule_version": "v1",
    }
    errors = validate(good(), ctx, SETTINGS, capital=550_000.0)
    assert any("last_price" in e for e in errors)


# --- rule_version: ctx の値がそのまま記録されること(AIの出力やハードコードに
# 依存しない)を確認するテスト ---


def test_enrich_sets_rule_version_from_ctx():
    ctx = dict(CTX, rule_version="v2")
    d = enrich(good(), ctx, SETTINGS)
    assert d["rule_version"] == "v2"


def test_enrich_does_not_hardcode_v1_when_ctx_has_different_version():
    # ctx が "v1" 以外を持っているのに "v1" になってしまうリグレッションを防ぐ。
    ctx = dict(CTX, rule_version="v3-experimental")
    d = enrich(good(), ctx, SETTINGS)
    assert d["rule_version"] == "v3-experimental"


def test_enrich_raises_when_ctx_missing_rule_version():
    # rule_version が無いまま "v1" 等にフォールバックすると、本当のバージョンが
    # わからないまま記録されてしまう(バージョン別成績比較が静かに壊れる)。
    # そのため、無い場合は早期に気づけるよう例外にする。
    ctx = {k: v for k, v in CTX.items() if k != "rule_version"}
    with pytest.raises(KeyError):
        enrich(good(), ctx, SETTINGS)


# --- 売り注文の検証: 保有していない銘柄・保有株数を超える売りを却下する ---


def good_sell(**overrides) -> dict:
    d = good(action="sell", quantity=10)
    d.update(overrides)
    return d


def test_accepts_sell_of_exact_held_quantity():
    # 境界: 保有株数ちょうどの売りは通る
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100, "bucket": "じっくり"}])
    assert validate(good_sell(quantity=100), ctx, SETTINGS, capital=550_000.0) == []


def test_rejects_sell_of_symbol_not_held():
    ctx = dict(CTX, positions=[])
    errors = validate(good_sell(), ctx, SETTINGS, capital=550_000.0)
    assert any("保有" in e for e in errors)


def test_rejects_sell_exceeding_held_quantity():
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100, "bucket": "じっくり"}])
    errors = validate(good_sell(quantity=200), ctx, SETTINGS, capital=550_000.0)
    # 却下メッセージに保有株数(100)と売却しようとした株数(200)の両方が含まれる
    assert any("100" in e and "200" in e for e in errors)


# --- I1: 型が誤ったフィールドを渡しても例外を投げず、却下メッセージを返すこと ---
# validate() はフィールドの存在は確認するが、これまで型を見ていなかった。
# entry_price=None は TypeError、"2450円" のような文字列は ValueError を
# validate() の外に漏らしていた。例外が main() まで伝播すると、そのバッチの
# 正当な提案も journal のコミットも道連れになる。


def test_rejects_entry_price_none_without_raising():
    errors = validate(good(entry_price=None), CTX, SETTINGS, capital=550_000.0)
    assert any("entry_price" in e for e in errors)


def test_rejects_entry_price_non_numeric_string():
    errors = validate(good(entry_price="2450円"), CTX, SETTINGS, capital=550_000.0)
    assert any("entry_price" in e and "2450円" in e for e in errors)


def test_rejects_quantity_non_numeric_string():
    errors = validate(good(quantity="many"), CTX, SETTINGS, capital=550_000.0)
    assert any("quantity" in e and "many" in e for e in errors)


def test_rejects_take_profit_none_without_raising():
    errors = validate(good(take_profit=None), CTX, SETTINGS, capital=550_000.0)
    assert any("take_profit" in e for e in errors)


def test_rejects_stop_loss_non_numeric_string_without_raising():
    errors = validate(good(stop_loss="安全圏"), CTX, SETTINGS, capital=550_000.0)
    assert any("stop_loss" in e and "安全圏" in e for e in errors)


# --- I2: quantity は1以上の整数であること。検証した値と保存する値を一致させる ---


def test_rejects_zero_quantity_on_buy():
    errors = validate(good(quantity=0), CTX, SETTINGS, capital=550_000.0)
    assert any("quantity" in e for e in errors)


def test_rejects_negative_quantity_on_buy():
    errors = validate(good(quantity=-5), CTX, SETTINGS, capital=550_000.0)
    assert any("quantity" in e for e in errors)


def test_rejects_zero_quantity_on_sell():
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 10}])
    errors = validate(good_sell(quantity=0), ctx, SETTINGS, capital=550_000.0)
    assert any("quantity" in e for e in errors)


def test_rejects_negative_quantity_on_sell():
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 10}])
    errors = validate(good_sell(quantity=-5), ctx, SETTINGS, capital=550_000.0)
    assert any("quantity" in e for e in errors)


def test_rejects_non_integer_quantity():
    # 100.9株のような端数は、切り捨てて100として静かに通してしまうと、
    # 検証した値(100)と decision に残った値(100.9)が食い違う原因になる。
    # 整数でなければそもそも却下する。
    errors = validate(good(quantity=100.9), CTX, SETTINGS, capital=550_000.0)
    assert any("quantity" in e and "整数" in e for e in errors)


def test_normalizes_whole_number_float_quantity_to_int():
    # quantity=100.0 (端数のない float) は受理してよいが、検証後に decision
    # 自体が持つ値は int の 100 に揃える。insert_proposals はこの decision の
    # 値をそのまま使うため、検証した値と保存される値を一致させるために必要。
    d = good(quantity=100.0)
    errors = validate(d, CTX, SETTINGS, capital=550_000.0)
    assert errors == []
    assert d["quantity"] == 100
    assert isinstance(d["quantity"], int)


# --- I5: 却下された提案は data_gaps に記録される(proposals には入れない) ---


def test_record_rejections_writes_to_data_gaps():
    from unittest.mock import patch

    from investment.jobs.apply_decision import record_rejections

    calls = []

    def fake_record_gap(conn, scope, detail):
        calls.append((scope, detail))

    with patch("investment.jobs.apply_decision.record_gap", side_effect=fake_record_gap):
        record_rejections(
            None,
            [("3993.T", ["stop_loss < entry_price < take_profit である必要があります"])],
        )

    assert len(calls) == 1
    scope, detail = calls[0]
    assert scope == "proposal_rejected:3993.T"
    assert "stop_loss < entry_price < take_profit である必要があります" in detail


# --- 修正2: 候補0件・decisions0件の日が data_gaps に記録されない問題 ---
#
# これまでは accepted も rejected も0件だと DB に一切書き込まずに終了していた
# (`if accepted or rejected:` で接続ごとスキップ)。つまり「候補が0件だった」
# 「候補はあったがAIが全部見送った」のどちらの日も無記録で消えていた。
# process() はこの2ケースを区別できる detail を付けて data_gaps に記録する。


def _patched_record_gap():
    from unittest.mock import patch

    calls = []

    def fake_record_gap(conn, scope, detail):
        calls.append((scope, detail))

    return patch("investment.jobs.apply_decision.record_gap", side_effect=fake_record_gap), calls


def test_process_records_gap_when_no_candidates_and_no_decisions():
    from investment.jobs.apply_decision import process

    ctx = {
        "can_open_new": True,
        "candidates": [],
        "positions": [],
        "rule_version": "v1",
    }
    patcher, calls = _patched_record_gap()
    with patcher:
        process(None, ctx, [], "journal/2026-09-07.md", SETTINGS, capital=550_000.0)

    assert len(calls) == 1
    scope, detail = calls[0]
    assert scope == "analysis:no_proposals"
    assert "候補 0" in detail  # 候補0件であることが detail から読み取れる


def test_process_records_gap_when_candidates_present_but_zero_decisions():
    from investment.jobs.apply_decision import process

    ctx = {
        "can_open_new": True,
        "candidates": [{"symbol": "3993.T", "last_price": 2450.0}],
        "positions": [],
        "rule_version": "v1",
    }
    patcher, calls = _patched_record_gap()
    with patcher:
        process(None, ctx, [], "journal/2026-09-07.md", SETTINGS, capital=550_000.0)

    assert len(calls) == 1
    scope, detail = calls[0]
    assert scope == "analysis:no_proposals"
    assert "候補 1" in detail  # 候補1件だったことが detail から読み取れる


def test_no_candidates_and_candidates_present_cases_are_distinguishable():
    from investment.jobs.apply_decision import process

    ctx_empty = {"can_open_new": True, "candidates": [], "positions": [], "rule_version": "v1"}
    ctx_with_candidates = {
        "can_open_new": True,
        "candidates": [{"symbol": "3993.T", "last_price": 2450.0}],
        "positions": [],
        "rule_version": "v1",
    }

    patcher1, calls1 = _patched_record_gap()
    with patcher1:
        process(None, ctx_empty, [], "journal/2026-09-07.md", SETTINGS, capital=550_000.0)

    patcher2, calls2 = _patched_record_gap()
    with patcher2:
        process(None, ctx_with_candidates, [], "journal/2026-09-07.md", SETTINGS, capital=550_000.0)

    detail_empty = calls1[0][1]
    detail_with_candidates = calls2[0][1]
    assert detail_empty != detail_with_candidates


def test_process_does_not_record_gap_when_something_accepted(tmp_path):
    """採用があった日は「候補0件」の記録を作らないこと。

    日誌チェックとは別の話なので、日誌は採用銘柄を含むものを用意する
    （用意しないと日誌チェックのほうが記録を作り、何を見ているのか
    分からないテストになる）。
    """
    from unittest.mock import patch

    from investment.jobs.apply_decision import process

    journal = _journal_for(tmp_path, ["3993.T"])
    patcher, calls = _patched_record_gap()
    with patcher, patch("investment.jobs.apply_decision.insert_proposals") as fake_insert:
        process(None, CTX, [good()], journal, SETTINGS, capital=550_000.0)

    assert calls == []
    fake_insert.assert_called_once()


def test_process_does_not_record_gap_when_something_rejected():
    from unittest.mock import patch

    from investment.jobs.apply_decision import process

    bad_decision = good()
    del bad_decision["stop_loss"]

    patcher, calls = _patched_record_gap()
    with patcher, patch("investment.jobs.apply_decision.record_rejections") as fake_reject:
        process(None, CTX, [bad_decision], "journal/2026-09-07.md", SETTINGS, capital=550_000.0)

    assert calls == []
    fake_reject.assert_called_once()


# --- insert_proposals: 検証済みの判断が実際にDBへ保存されることの確認 ---

TEST_URL = os.environ.get("DATABASE_URL_TEST")


@pytest.fixture
def conn():
    if not TEST_URL:
        pytest.skip("DATABASE_URL_TEST が未設定のためスキップします")
    with connect(TEST_URL) as c:
        apply_migrations(c)
        with c.cursor() as cur:
            cur.execute(
                "TRUNCATE fundamentals, trades, positions, proposals, cash, data_gaps, fills"
            )
        c.commit()
        yield c


@pytest.mark.integration
def test_insert_proposals_saves_validated_decision(conn):
    d = good()
    d["required_win_rate"] = 0.2667
    # enrich() を経由しない単体テストなので、enrich が付与するはずの
    # rule_version をここで手動で用意する(required_win_rate と同じ扱い)。
    d["rule_version"] = "v1"

    inserted = insert_proposals(conn, [d], journal_path="journal/2026-09-07.md")

    assert inserted == 1
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM proposals")
        rows = cur.fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "3993.T"
    assert row["action"] == "buy"
    assert row["quantity"] == 100
    assert row["confidence"] == "mid"
    assert row["outcome"] == "pending"
    assert row["journal_path"] == "journal/2026-09-07.md"
    assert row["rule_version"] == "v1"
    # どちらの枠で買ったかを記録する。これが無いと枠ごとの成績を比べられない
    assert row["bucket"] == "じっくり"


@pytest.mark.integration
def test_insert_proposals_saves_rule_version_from_context_not_hardcoded_v1(conn):
    # 修正1の回帰テスト: ctx["rule_version"] が "v2" のとき、DBに保存される
    # 値も "v2" になること("v1" にならないこと)を確認する。
    d = good()
    d["required_win_rate"] = 0.2667
    d["rule_version"] = "v2"

    insert_proposals(conn, [d], journal_path="journal/2026-09-07.md")

    with conn.cursor() as cur:
        cur.execute("SELECT rule_version FROM proposals")
        row = cur.fetchone()
    assert row["rule_version"] == "v2"


@pytest.mark.integration
def test_record_rejections_saves_to_data_gaps_table(conn):
    # I5: 却下された提案は proposals には入れず(quantity<=0 等、この表の
    # CHECK 制約に違反しうる値を持つ場合があるため)、data_gaps に却下理由の
    # 全文を残す。journal には却下の経緯が文章で残るのに、DB側の記録が
    # 存在しないと記録同士が食い違ってしまう。
    from investment.jobs.apply_decision import record_rejections

    record_rejections(conn, [("9999.T", ["エラーA", "エラーB"])])

    with conn.cursor() as cur:
        cur.execute("SELECT scope, detail FROM data_gaps")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["scope"] == "proposal_rejected:9999.T"
    assert "エラーA" in rows[0]["detail"]
    assert "エラーB" in rows[0]["detail"]


# --- 売買単位（単元株）の検証 -----------------------------------------------


def test_rejects_quantity_that_is_not_a_multiple_of_the_lot_size():
    """日本株で37株のような単元未満の株数は注文できないので却下する。

    AI は「上限金額 ÷ 株価」で株数を出しがちだが、日本株は100株単位でしか
    注文できない。この検証が無いと、実際には発注できない提案が通ってしまう。
    """
    errors = validate(good(quantity=137), CTX, SETTINGS, capital=550_000.0)
    assert any("100株単位" in e for e in errors)


def test_rejects_buy_when_one_lot_exceeds_the_position_limit():
    """1単元（100株）ですら上限金額を超える銘柄は買えないので却下する。

    株価2,704円だと100株で270,400円になり、1銘柄上限137,500円を超える。
    実際に 2026-09-07 の初回運用で AI が提案し、発注できなかったケース。
    """
    ctx = dict(CTX, candidates=[{"symbol": "3723.T", "last_price": 2704.0}])
    d = good(
        symbol="3723.T",
        entry_price=2704.0,
        take_profit=2704.0 * 1.22,
        stop_loss=2704.0 * 0.92,
        quantity=100,
    )
    errors = validate(d, ctx, SETTINGS, capital=550_000.0)
    assert any("上限" in e for e in errors)


def test_us_stocks_are_not_subject_to_the_100_share_unit():
    """米国株は1株から買えるので、単元の制約をかけない。"""
    ctx = dict(CTX, candidates=[{"symbol": "AAPL", "last_price": 200.0}])
    d = good(
        symbol="AAPL",
        entry_price=200.0,
        take_profit=244.0,
        stop_loss=184.0,
        quantity=137,  # 日本株なら却下される端数だが、米国株では正当
    )
    assert validate(d, ctx, SETTINGS, capital=550_000.0) == []


# --- 却下理由が事実と合っていること -----------------------------------------


def test_rejection_says_risk_limit_when_the_stop_is_too_wide():
    """損切り幅が広くて買えない場合に、金額の上限のせいだと言わないこと。

    株数が0になる理由は2つある。(1) 1銘柄の金額上限、(2) 1回の損失上限。
    どちらでも「金額の上限を超える」と言っていたため、100株で130,000円
    （上限137,500円以内）なのに「上限を超える」という、それ自体で
    矛盾しているメッセージが出ていた。
    """
    # 損切りが -9.2%（ルールの -8% より広い）。金額は上限内だが損失が上限を超える
    d = good(entry_price=1300.0, take_profit=1300.0 * 1.22, stop_loss=1180.0, quantity=100)
    ctx = dict(CTX, candidates=[{"symbol": "3993.T", "last_price": 1300.0}])
    errors = validate(d, ctx, SETTINGS, capital=550_000.0)

    assert any("損失" in e for e in errors), errors
    # 金額の上限を超えていないのに「金額の上限」と言ってはいけない
    assert not any("1銘柄の上限" in e for e in errors), errors


def test_rejection_says_position_limit_when_one_lot_costs_too_much():
    """金額の上限で買えない場合は、金額の上限だと言うこと。"""
    d = good(entry_price=2704.0, take_profit=2704.0 * 1.22, stop_loss=2704.0 * 0.92,
             quantity=100, symbol="3723.T")
    ctx = dict(CTX, candidates=[{"symbol": "3723.T", "last_price": 2704.0}])
    errors = validate(d, ctx, SETTINGS, capital=550_000.0)

    assert any("1銘柄の上限" in e for e in errors), errors


# --- 売りも100株単位 --------------------------------------------------------


def test_rejects_partial_sell_that_is_not_a_multiple_of_the_lot_size():
    """保有100株のうち50株だけ売る、という注文は出せないので却下する。

    買いと同じ理由。単元未満株（S株）は逆指値が使えないため v2 で使わないと
    決めており、売りも100株単位でしか注文できない。
    """
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100}])
    errors = validate(good_sell(quantity=50), ctx, SETTINGS, capital=550_000.0)
    assert any("100株単位" in e for e in errors), errors


def test_accepts_partial_sell_of_a_whole_number_of_lots():
    """300株保有のうち100株だけ売るのは正当。"""
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 300, "bucket": "じっくり"}])
    assert validate(good_sell(quantity=100), ctx, SETTINGS, capital=550_000.0) == []


def test_accepts_selling_the_entire_holding_even_if_it_is_not_a_whole_lot():
    """保有株数そのものが単元未満でも、全部売るのは常に可能。

    端株（株式分割などで生じる100株未満の保有）は、まとめてなら売却できる。
    「全部売る」を却下すると、持ち続けるしかなくなってしまう。
    """
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 37, "bucket": "じっくり"}])
    assert validate(good_sell(quantity=37), ctx, SETTINGS, capital=550_000.0) == []


# --- AIの判断ファイルが無い場合 ---------------------------------------------
# 2026-09-07 の3回目の実行で、AIは判断も日誌も完成させたのに、
# 「使ったターン数が設定の上限を超えた」という理由でステップが失敗扱いになり、
# その結果 保存ステップ自体が実行されず、完成していた判断が捨てられた。
# 保存ステップは常に実行するように変更したため、判断ファイルが本当に無い
# ケース（AIが途中で止まった場合）を、例外ではなく記録として扱う必要がある。


def test_load_decision_records_a_gap_when_the_file_is_missing(tmp_path):
    """判断ファイルが無い場合、例外ではなく「無かった」という記録を返すこと。"""
    from investment.jobs.apply_decision import load_decision

    decisions, journal_path, missing = load_decision(tmp_path / "decision.json")

    assert missing is not None
    assert missing[0] == "decision:missing"          # data_gaps に残す分類
    assert "decision.json" in missing[1]             # 人が読む説明
    assert decisions == []
    assert journal_path == ""


def test_load_decision_records_a_gap_when_the_file_is_broken(tmp_path):
    """壊れたJSONも、例外ではなく記録として扱うこと。

    AIが途中で止まると、書きかけのJSONが残ることがある。
    """
    from investment.jobs.apply_decision import load_decision

    p = tmp_path / "decision.json"
    p.write_text('{"decisions": [', encoding="utf-8")
    decisions, _, missing = load_decision(p)

    assert missing is not None
    assert decisions == []


def test_load_decision_reads_a_valid_file(tmp_path):
    """正常なファイルはそのまま読める。"""
    from investment.jobs.apply_decision import load_decision

    p = tmp_path / "decision.json"
    p.write_text(
        '{"journal_path": "journal/2026-09-07.md", "decisions": [{"symbol": "3993.T"}]}',
        encoding="utf-8",
    )
    decisions, journal_path, missing = load_decision(p)

    assert missing is None
    assert decisions == [{"symbol": "3993.T"}]
    assert journal_path == "journal/2026-09-07.md"


# --- 枠（じっくり / 回転）の検証 --------------------------------------------
# v3 から、どちらの枠で買うかによって利確・損切りの幅が変わる。
# これまで validate は利確・損切りの幅を一切見ていなかったため、
# AIがルールと違う幅を書いてもそのまま通っていた。


def _ctx_with_buckets(**overrides) -> dict:
    ctx = dict(CTX)
    ctx["buckets"] = [
        {"name": "じっくり", "take_profit_pct": 0.22, "stop_loss_pct": 0.08,
         "max_holding_days": None, "slots": 2, "used": 0, "free": 2},
        {"name": "回転", "take_profit_pct": 0.10, "stop_loss_pct": 0.05,
         "max_holding_days": 10, "slots": 2, "used": 0, "free": 2},
    ]
    ctx.update(overrides)
    return ctx


def _patient(**overrides) -> dict:
    """じっくり枠の正しい提案。1,200円 × 100株。"""
    d = good(bucket="じっくり", entry_price=1200.0, take_profit=1464.0,
             stop_loss=1104.0, quantity=100)
    d.update(overrides)
    return d


def _fast(**overrides) -> dict:
    """回転枠の正しい提案。1,200円 × 100株、+10%/-5%。"""
    d = good(bucket="回転", entry_price=1200.0, take_profit=1320.0,
             stop_loss=1140.0, quantity=100)
    d.update(overrides)
    return d


def test_validate_uses_the_capital_it_is_given_not_the_context():
    """総資金は呼び出し側（データベースを読んだ側）から受け取ること。

    context.json は AI が書き換えられる場所にあるので、そこの数字で
    株数の上限を決めてはいけない。枠の利確・損切り幅と同じ理由。
    """
    ctx = _ctx_with_buckets()
    # コンテキスト側の総資金を10倍に改ざんしても、判定は引数の値で行われる
    ctx["constraints"] = dict(ctx.get("constraints", {}), total_capital=5_500_000)

    # 550,000円の25% = 137,500円。1,200円 × 200株 = 240,000円は上限超え
    errors = validate(_patient(quantity=200), ctx, SETTINGS, capital=550_000.0)
    assert any("上限" in e for e in errors), errors


def test_validate_allows_more_shares_when_the_capital_has_grown():
    """資金が増えたら、買える株数も増えること。"""
    ctx = _ctx_with_buckets()
    # 1,100,000円の25% = 275,000円。1,200円 × 200株 = 240,000円は収まる
    assert validate(_patient(quantity=200), ctx, SETTINGS, capital=1_100_000.0) == []


def test_accepts_a_correct_patient_proposal():
    assert validate(_patient(), _ctx_with_buckets(), SETTINGS, capital=550_000.0) == []


def test_accepts_a_correct_fast_proposal():
    assert validate(_fast(), _ctx_with_buckets(), SETTINGS, capital=550_000.0) == []


def test_rejects_a_missing_bucket():
    d = _patient()
    del d["bucket"]
    assert any("bucket" in e for e in validate(d, _ctx_with_buckets(), SETTINGS, capital=550_000.0))


def test_rejects_an_unknown_bucket():
    errors = validate(_patient(bucket="なんとなく"), _ctx_with_buckets(), SETTINGS, capital=550_000.0)
    assert any("なんとなく" in e for e in errors)


def test_rejects_a_take_profit_that_does_not_match_the_bucket():
    """回転枠(+10%)なのに +22% の利確を書いたら却下する。

    これを通すと「枠」が名前だけになり、どちらが効いているかを
    比較できなくなる。
    """
    errors = validate(_fast(take_profit=1464.0), _ctx_with_buckets(), SETTINGS, capital=550_000.0)
    assert any("take_profit" in e and "回転" in e for e in errors), errors


def test_rejects_a_stop_loss_that_does_not_match_the_bucket():
    errors = validate(_fast(stop_loss=1104.0), _ctx_with_buckets(), SETTINGS, capital=550_000.0)
    assert any("stop_loss" in e and "回転" in e for e in errors), errors


def test_allows_rounding_to_the_nearest_yen():
    """1円単位に丸めた値は許容する。899 × 1.22 = 1096.78 のような端数が出るため。"""
    e = 899.0
    d = _patient(entry_price=e, take_profit=round(e * 1.22, 2),
                 stop_loss=round(e * 0.92, 2), quantity=100)
    ctx = _ctx_with_buckets(candidates=[{"symbol": "3993.T", "last_price": 899.0}])
    assert validate(d, ctx, SETTINGS, capital=550_000.0) == []


def test_rejects_a_buy_when_the_bucket_has_no_free_slot():
    """枠が埋まっている側には提案できない。

    全体では空きがあっても、その枠が埋まっていれば買えない。
    """
    ctx = _ctx_with_buckets()
    ctx["buckets"] = [
        {**ctx["buckets"][0], "used": 2, "free": 0},   # じっくり枠は満杯
        ctx["buckets"][1],                              # 回転枠は空いている
    ]
    errors = validate(_patient(), ctx, SETTINGS, capital=550_000.0)
    assert any("じっくり" in e and "空き" in e for e in errors), errors
    # 回転枠なら通る
    assert validate(_fast(), ctx, SETTINGS, capital=550_000.0) == []


def test_the_bucket_rules_come_from_the_code_not_from_the_context_file():
    """利確・損切りの幅は、AIが書き換えられるファイルではなくコードから取ること。

    context.json は AI が Write できる場所にある。そこに書かれた枠の幅で
    検証すると、AIが自分の数字を自分の数字で検証することになり、
    「AIの出力を信用しない」というこの仕組みの前提が崩れる。
    枠の幅は investment.config.BUCKETS が唯一の出所である。
    """
    # context.json 側の枠の幅が改ざんされていても、コード側の幅で判定する
    ctx = _ctx_with_buckets()
    ctx["buckets"] = [
        {**ctx["buckets"][0], "take_profit_pct": 0.50, "stop_loss_pct": 0.01},
        ctx["buckets"][1],
    ]
    # 改ざんされた幅（+50%/-1%）に沿った提案は、却下されなければならない
    d = _patient(take_profit=1800.0, stop_loss=1188.0)
    errors = validate(d, ctx, SETTINGS, capital=550_000.0)
    assert any("take_profit" in e for e in errors), errors

    # 本来の幅（+22%/-8%）に沿った提案は通る
    assert validate(_patient(), ctx, SETTINGS, capital=550_000.0) == []


def test_the_free_slot_count_still_comes_from_the_context():
    """空き枠の数は、そのときの保有状況なのでコンテキストから取る。"""
    ctx = _ctx_with_buckets()
    ctx["buckets"] = [{**ctx["buckets"][0], "used": 2, "free": 0}, ctx["buckets"][1]]
    assert any("空き" in e for e in validate(_patient(), ctx, SETTINGS, capital=550_000.0))


# --- 売りに bucket を求めない（保有から決まるため） --------------------------


def test_sell_does_not_require_the_ai_to_state_the_bucket():
    """売りの枠は、その銘柄を買ったときの枠で決まっている。AIに書かせない。

    AIに書かせると、保有と違う枠を書いたり、書き忘れたりする。
    どちらも保有側に正しい答えがあるのだから、そちらから取る。
    """
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100, "bucket": "回転"}])
    d = good_sell(quantity=100)
    del d["bucket"]

    assert validate(d, ctx, SETTINGS, capital=550_000.0) == []
    # 検証の副作用として、保有の枠が decision に入る（保存時に使う）
    assert d["bucket"] == "回転"


def test_sell_overwrites_a_bucket_the_ai_guessed_wrong():
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100, "bucket": "回転"}])
    d = good_sell(quantity=100, bucket="じっくり")   # AIの書いた枠は間違い

    assert validate(d, ctx, SETTINGS, capital=550_000.0) == []
    assert d["bucket"] == "回転"   # 保有側が正


def test_sell_is_rejected_when_the_position_has_no_bucket():
    """保有に枠の記録が無ければ却下する。勝手に決めない。

    枠が分からないまま保存すると、枠ごとの成績集計から黙って漏れる。
    """
    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100}])
    errors = validate(good_sell(quantity=100), ctx, SETTINGS, capital=550_000.0)
    assert any("枠" in e for e in errors), errors


# --- 1回の判断の中で、枠の数を超えて買えないこと ----------------------------
# 検証は1件ずつ独立に同じ context を見るため、そのままでは
# 「空き2の枠に3件出したら3件とも通る」という状態になっていた。
# 空き枠の表示を直すだけでは何も止まらない。通した件数を数える必要がある。


def test_validate_counts_what_was_already_accepted_in_this_batch():
    """このバッチで既に通した件数を差し引いて空きを見ること。"""
    ctx = _ctx_with_buckets()   # じっくり枠の空きは2

    assert validate(_patient(symbol="3993.T"), ctx, SETTINGS, capital=550_000.0, taken={}) == []
    assert validate(_patient(symbol="3993.T"), ctx, SETTINGS, capital=550_000.0, taken={"じっくり": 1}) == []
    errors = validate(_patient(symbol="3993.T"), ctx, SETTINGS, capital=550_000.0, taken={"じっくり": 2})
    assert any("空き" in e for e in errors), errors


def test_validate_stops_at_the_overall_limit_across_buckets():
    """枠ごとに空きがあっても、全体の上限を超えたら止めること。

    保有3件（全体4枠中の残り1）で、じっくりと回転に1件ずつ出すと、
    枠ごとには空いていても合計5件になる。
    """
    ctx = _ctx_with_buckets(positions=[{"symbol": f"{i}.T"} for i in range(3)])
    ctx["buckets"] = [
        {**ctx["buckets"][0], "used": 0, "free": 1},
        {**ctx["buckets"][1], "used": 3, "free": 0},
    ]
    # 1件目は通る
    assert validate(_patient(), ctx, SETTINGS, capital=550_000.0, taken={}) == []
    # 2件目は全体の上限で止まる
    errors = validate(_fast(), ctx, SETTINGS, capital=550_000.0, taken={"じっくり": 1})
    assert any("空き" in e for e in errors), errors


def _journal_for(tmp_path, symbols) -> str:
    """採用する銘柄が出てくる日誌を用意する（日誌チェックを通すため）。"""
    j = tmp_path / "journal.md"
    j.write_text("# 判断\n\n" + "\n".join(f"{s} について" for s in symbols), encoding="utf-8")
    return str(j)


def test_process_rejects_the_third_buy_into_a_two_slot_bucket(tmp_path):
    """本番の経路（process）で、空き2の枠に3件出したら3件目が却下されること。

    これが指摘の本体。validate を1件ずつ呼ぶだけでは止まらなかった。
    """
    from unittest.mock import patch

    ctx = _ctx_with_buckets()
    ctx["candidates"] = [{"symbol": f"{i}.T", "last_price": 1200.0} for i in range(1, 4)]
    decisions = [_patient(symbol=f"{i}.T") for i in range(1, 4)]
    journal = _journal_for(tmp_path, [f"{i}.T" for i in range(1, 4)])

    with (
        patch("investment.jobs.apply_decision.insert_proposals") as insert,
        patch("investment.jobs.apply_decision.record_rejections") as reject,
    ):
        accepted, rejected = process(None, ctx, decisions, journal, SETTINGS, capital=550_000.0)

    assert (accepted, rejected) == (2, 1)
    assert [d["symbol"] for d in insert.call_args.args[1]] == ["1.T", "2.T"]
    # 却下された事実は data_gaps に残る（黙って捨てない）
    reject.assert_called_once()
    assert reject.call_args.args[1][0][0] == "3.T"


def test_process_counts_the_two_buckets_separately(tmp_path):
    """枠が違えば、それぞれの空き枠まで通ること。"""
    from unittest.mock import patch

    ctx = _ctx_with_buckets()
    ctx["candidates"] = [{"symbol": f"{i}.T", "last_price": 1200.0} for i in range(1, 5)]
    decisions = [
        _patient(symbol="1.T"), _patient(symbol="2.T"),
        _fast(symbol="3.T"), _fast(symbol="4.T"),
    ]

    journal = _journal_for(tmp_path, [f"{i}.T" for i in range(1, 5)])
    with (
        patch("investment.jobs.apply_decision.insert_proposals"),
        patch("investment.jobs.apply_decision.record_rejections"),
    ):
        accepted, rejected = process(None, ctx, decisions, journal, SETTINGS, capital=550_000.0)

    assert (accepted, rejected) == (4, 0)


def test_process_records_a_sell_whose_bucket_disagreed_with_the_holding(tmp_path):
    """AIが保有と違う枠を書いたら、保存はするが記録も残すこと。

    保有側が正なので上書きして保存する（却下しない）。ただし黙って直すと、
    AIの間違いという最も価値のある材料が消える。
    """
    from unittest.mock import patch

    ctx = dict(CTX, positions=[{"symbol": "3993.T", "quantity": 100, "bucket": "回転"}])
    d = good_sell(quantity=100, bucket="じっくり")  # AIの書いた枠は間違い

    with (
        patch("investment.jobs.apply_decision.insert_proposals") as insert,
        patch("investment.jobs.apply_decision.record_rejections") as record,
    ):
        accepted, rejected = process(
            None, ctx, [d], _journal_for(tmp_path, ["3993.T"]), SETTINGS, capital=550_000.0
        )

    assert (accepted, rejected) == (1, 0)          # 却下はしない
    assert insert.call_args.args[1][0]["bucket"] == "回転"   # 保有側で保存
    record.assert_called_once()                     # 食い違いは記録される
    assert "回転" in record.call_args.args[1][0][1][0]


def test_price_tolerance_scales_with_the_currency_of_the_symbol():
    """許容誤差の下限を、値段の刻み幅の半分にすること。

    日本株は1円刻みなので0.5、米国株は1セント刻みなので0.005。
    どちらも0.5にすると、50ドルの株で +21%〜+23% が通ってしまい、
    円建てで直したはずのズレがドル建てで再発する。
    """
    from investment.jobs.apply_decision import _price_tolerance

    # 日本株: 安い株でも1円の四捨五入を吸収できる
    assert _price_tolerance(100.0, "7203.T") == 0.5
    # 日本株: 高くなれば価格に比例した幅になる
    assert _price_tolerance(1000.0, "7203.T") == 2.0
    # 米国株: 下限は1セントの半分。50ドルなら比例分(0.1)のほうが大きい
    assert _price_tolerance(1.0, "AAPL") == 0.005
    assert _price_tolerance(50.0, "AAPL") == 0.1


# --- 日誌が書かれたかを確かめる ---------------------------------------------
# 2026-09-07 に、AIが decision.json は書いたのに journal を書かないまま
# 「成功」で終わった。判断は残るが、なぜそう判断したかの記録が残らない。
# 判断そのものは正しいので却下はしないが、書かれていない事実は記録する。


def test_journal_check_passes_when_the_journal_mentions_every_symbol(tmp_path):
    from investment.jobs.apply_decision import check_journal_covers

    j = tmp_path / "j.md"
    j.write_text("# 判断\n\n156A.T を買う。9344.T も買う。\n", encoding="utf-8")

    assert check_journal_covers(j, [{"symbol": "156A.T"}, {"symbol": "9344.T"}]) is None


def test_journal_check_reports_symbols_the_journal_never_mentions(tmp_path):
    from investment.jobs.apply_decision import check_journal_covers

    j = tmp_path / "j.md"
    j.write_text("# 判断\n\n156A.T を買う。\n", encoding="utf-8")

    gap = check_journal_covers(j, [{"symbol": "156A.T"}, {"symbol": "9344.T"}])
    assert gap is not None
    assert gap[0] == "journal:incomplete"
    assert "9344.T" in gap[1]
    assert "156A.T" not in gap[1]   # 書かれている銘柄は挙げない


def test_journal_check_reports_a_missing_file(tmp_path):
    from investment.jobs.apply_decision import check_journal_covers

    gap = check_journal_covers(tmp_path / "ない.md", [{"symbol": "156A.T"}])
    assert gap is not None
    assert gap[0] == "journal:missing"


def test_journal_check_is_skipped_when_nothing_was_accepted(tmp_path):
    """採用が0件なら、日誌に載る銘柄も無い。"""
    from investment.jobs.apply_decision import check_journal_covers

    assert check_journal_covers(tmp_path / "ない.md", []) is None


def test_process_records_a_gap_when_the_journal_is_missing(tmp_path):
    """判断は保存するが、日誌が無いことは記録に残すこと。

    2026-09-07 の実行で、AIが decision.json だけ書いて日誌を書かなかった。
    ステップは成功扱いで、誰も気づかないまま終わった。
    """
    from unittest.mock import patch

    ctx = _ctx_with_buckets()
    ctx["candidates"] = [{"symbol": "1.T", "last_price": 1200.0}]

    with (
        patch("investment.jobs.apply_decision.insert_proposals"),
        patch("investment.jobs.apply_decision.record_rejections"),
        patch("investment.jobs.apply_decision.record_gap") as gap,
    ):
        accepted, rejected = process(
            None, ctx, [_patient(symbol="1.T")], str(tmp_path / "ない.md"), SETTINGS, capital=550_000.0
        )

    assert (accepted, rejected) == (1, 0)   # 判断そのものは保存する
    gap.assert_called_once()
    assert gap.call_args.kwargs["scope"] == "journal:missing"


def test_journal_check_detects_a_stale_journal_from_an_earlier_run(tmp_path):
    """今回書かれていない日誌を見抜くこと。

    2026-09-07 に、AIが日誌を書かないまま終わったのに検知が素通りした。
    同じ日の前の実行で書かれた日誌に、たまたま同じ銘柄が載っていたため。
    「銘柄名が書いてあるか」ではなく「今回書かれたか」を見る必要がある。
    """
    import os

    from investment.jobs.apply_decision import check_journal_covers

    journal = tmp_path / "j.md"
    journal.write_text("156A.T について（前回の実行で書いたもの）", encoding="utf-8")
    before = tmp_path / "context.json"
    before.write_text("{}", encoding="utf-8")
    # 基準ファイルのほうが新しい ＝ 日誌は今回書かれていない
    os.utime(journal, (1000, 1000))
    os.utime(before, (2000, 2000))

    gap = check_journal_covers(journal, [{"symbol": "156A.T"}], written_after=before)
    assert gap is not None
    assert gap[0] == "journal:stale"
    assert "前の実行" in gap[1] or "今回" in gap[1]


def test_journal_check_passes_when_the_journal_was_written_this_run(tmp_path):
    import os

    from investment.jobs.apply_decision import check_journal_covers

    journal = tmp_path / "j.md"
    journal.write_text("156A.T について", encoding="utf-8")
    before = tmp_path / "context.json"
    before.write_text("{}", encoding="utf-8")
    os.utime(before, (1000, 1000))
    os.utime(journal, (2000, 2000))   # 日誌のほうが新しい

    assert check_journal_covers(journal, [{"symbol": "156A.T"}], written_after=before) is None


def _write_build_files(tmp_path, decisions: list[dict]) -> None:
    """main() が読む build/context.json と build/decision.json を用意する。

    brief の元テストは json.loads / Path.read_text をモジュール単位で
    差し替えていたが、それだと無関係な処理まで巻き込んで壊れやすい
    （標準ライブラリの関数を丸ごと差し替えるため）。ここでは実際に
    一時ディレクトリへファイルを書き、main() に本物のファイルを読ませる。
    検証する振る舞い（提案があれば通知する／0件なら通知しない）は変えない。
    """
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    (build_dir / "context.json").write_text("{}", encoding="utf-8")
    (build_dir / "decision.json").write_text(
        json.dumps({"decisions": decisions, "journal_path": "journal/x.md"}),
        encoding="utf-8",
    )


def test_main_notifies_when_there_are_proposals(tmp_path, monkeypatch):
    """買うべき提案が出たときは通知する。"""
    monkeypatch.chdir(tmp_path)
    _write_build_files(tmp_path, [{"symbol": "1111.T"}])

    with (
        patch("investment.jobs.apply_decision.connect"),
        patch("investment.jobs.apply_decision.select_capital", return_value=550_000.0),
        patch("investment.jobs.apply_decision.process", return_value=(1, 0)),
        patch("investment.jobs.apply_decision.notify_send") as notify,
    ):
        main()

    notify.assert_called_once()
    assert "1" in notify.call_args.kwargs["title"]


def test_main_does_not_notify_when_there_are_no_proposals(tmp_path, monkeypatch):
    """提案が0件のときは通知しない。動く必要がないため。"""
    monkeypatch.chdir(tmp_path)
    _write_build_files(tmp_path, [])

    with (
        patch("investment.jobs.apply_decision.connect"),
        patch("investment.jobs.apply_decision.select_capital", return_value=550_000.0),
        patch("investment.jobs.apply_decision.process", return_value=(0, 0)),
        patch("investment.jobs.apply_decision.notify_send") as notify,
    ):
        main()

    notify.assert_not_called()
