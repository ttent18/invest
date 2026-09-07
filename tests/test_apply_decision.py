import os

import pytest

from investment.config import SETTINGS
from investment.db import apply_migrations, connect
from investment.jobs.apply_decision import insert_proposals, validate

CTX = {
    "can_open_new": True,
    # last_price は good() の entry_price(2450.0) と整合させてある。
    # ズレのテストは各テストの中で candidates を差し替えて行う。
    "candidates": [{"symbol": "3993.T", "last_price": 2450.0}],
    "rule_version": "v1",
}


def good(**overrides) -> dict:
    d = {
        "symbol": "3993.T",
        "action": "buy",
        "entry_price": 2450.0,
        "take_profit": 2989.0,   # +22%
        "stop_loss": 2254.0,     # -8%
        "quantity": 33,
        "rationale": "決算で売上が伸びている",
        "scenario": "2〜3週間で調整前の水準に戻る想定",
        "confidence": "mid",
        "strategy_tag": "短期モメンタム",
    }
    d.update(overrides)
    return d


def test_valid_decision_has_no_errors():
    assert validate(good(), CTX, SETTINGS) == []


def test_rejects_missing_stop_loss():
    d = good()
    del d["stop_loss"]
    errors = validate(d, CTX, SETTINGS)
    assert any("stop_loss" in e for e in errors)


def test_rejects_symbol_not_in_candidates():
    errors = validate(good(symbol="7203.T"), CTX, SETTINGS)
    assert any("候補に含まれていません" in e for e in errors)


def test_rejects_quantity_over_position_limit():
    # 上限15% = 82,500円 → 2450円で33株が上限。34株は超過
    errors = validate(good(quantity=34), CTX, SETTINGS)
    assert any("上限" in e for e in errors)


def test_rejects_when_no_room_for_new_position():
    ctx = dict(CTX, can_open_new=False)
    errors = validate(good(), ctx, SETTINGS)
    assert any("同時保有" in e for e in errors)


def test_rejects_stop_loss_above_entry():
    errors = validate(good(stop_loss=2500.0), CTX, SETTINGS)
    assert any("stop_loss" in e for e in errors)


def test_rejects_unknown_confidence():
    errors = validate(good(confidence="とても高い"), CTX, SETTINGS)
    assert any("confidence" in e for e in errors)


def test_rejects_empty_rationale():
    errors = validate(good(rationale="  "), CTX, SETTINGS)
    assert any("rationale" in e for e in errors)


# --- entry_price と実際の株価(last_price)のズレを検証するテスト ---
# AIは entry_price を自己申告するだけで、それが実際の株価と近いかは保証されない。
# last_price(市場から取得した実際の価格)と±10%を超えて離れていたら却下する。


def test_accepts_entry_price_matching_last_price():
    # entry_price(2450.0) は candidates の last_price(2450.0) と一致
    assert validate(good(), CTX, SETTINGS) == []


def test_accepts_entry_price_at_plus_10_percent_boundary():
    last_price = 2450.0
    entry = last_price * 1.10  # 2695.0 ちょうど。境界は許容する
    # sl/tp も entry からの -8%/+22% に合わせて計算し直す(順序制約を壊さないため)
    d = good(
        entry_price=entry,
        take_profit=entry * 1.22,
        stop_loss=entry * 0.92,
        # 上限15% = 82,500円 → 2695円で30.6株 → 30株が上限
        quantity=30,
    )
    assert validate(d, CTX, SETTINGS) == []


def test_accepts_entry_price_at_minus_10_percent_boundary():
    last_price = 2450.0
    entry = last_price * 0.90  # 2205.0 ちょうど。境界は許容する
    d = good(
        entry_price=entry,
        take_profit=entry * 1.22,
        stop_loss=entry * 0.92,
        # 上限15% = 82,500円 → 2205円で37.4株 → 37株が上限
        quantity=37,
    )
    assert validate(d, CTX, SETTINGS) == []


def test_rejects_entry_price_far_from_last_price():
    # 実際の株価は2450円なのに、買値100円は明らかにおかしい(捏造や勘違いの疑い)
    errors = validate(good(entry_price=100.0), CTX, SETTINGS)
    assert any("2450" in e and "100" in e for e in errors)


def test_rejects_when_last_price_missing_from_candidate():
    # last_price キーが無い候補は異常。あるはずのものが無いので却下する
    ctx = {
        "can_open_new": True,
        "candidates": [{"symbol": "3993.T"}],
        "rule_version": "v1",
    }
    errors = validate(good(), ctx, SETTINGS)
    assert any("last_price" in e for e in errors)


# --- insert_proposals: 検証済みの判断が実際にDBへ保存されることの確認 ---

TEST_URL = os.environ.get("DATABASE_URL_TEST")


@pytest.fixture
def conn():
    if not TEST_URL:
        pytest.skip("DATABASE_URL_TEST が未設定のためスキップします")
    with connect(TEST_URL) as c:
        apply_migrations(c)
        with c.cursor() as cur:
            cur.execute("TRUNCATE fundamentals, trades, positions, proposals, cash, data_gaps")
        c.commit()
        yield c


@pytest.mark.integration
def test_insert_proposals_saves_validated_decision(conn):
    d = good()
    d["required_win_rate"] = 0.2667

    inserted = insert_proposals(conn, [d], journal_path="journal/2026-09-07.md")

    assert inserted == 1
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM proposals")
        rows = cur.fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["symbol"] == "3993.T"
    assert row["action"] == "buy"
    assert row["quantity"] == 33
    assert row["confidence"] == "mid"
    assert row["outcome"] == "pending"
    assert row["journal_path"] == "journal/2026-09-07.md"
