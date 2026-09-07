# 計画2-A: 記録の輪を閉じる — 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 「買った／売った」を1行記録すれば、保有・現金・取引履歴・枠ごとの成績が正しく更新され、動くべきときだけスマホに通知が飛ぶところまでを、Cloudflare に一切触らずに完成させる。

**Architecture:** 利用者の申告は `fills` テーブルに生のまま追記する（計算しない）。Python の `apply_fills` ジョブがそれを読んで `trades` / `positions` / `cash` に反映する（計算はここだけ）。総資金はコードの直書きをやめ、現金と保有の取得原価から計算する。通知は Python から `pywebpush` で送る。

**Tech Stack:** Python 3.12 / psycopg 3 / PostgreSQL (Neon) / pytest / pywebpush / GitHub Actions

**Spec:** [docs/superpowers/specs/2026-09-07-pwa-design.md](../specs/2026-09-07-pwa-design.md)
（親設計: [docs/superpowers/specs/2026-09-07-ai-investment-learning-design.md](../specs/2026-09-07-ai-investment-learning-design.md)）

**この計画に含まれないもの:** Cloudflare Pages、Pages Functions、画面（HTML/JavaScript）、通知の購読登録の画面。それらは計画2-B で行う。

## Global Constraints

- **投資用語を説明なしに使わない。** 利用者は投資初心者。コメント・ログ出力・ドキュメントの日本語は、専門家でない人が読んで分かる言葉で書く。英語の識別子（関数名・変数名）はそのままでよい
- **計算は Python だけが行う。** JavaScript 側に同じ計算を書かない
- **データを黙って落とさない。** 反映できなかった記録は消さず、理由を残す
- **`fills` は追記のみ。** 一度入れた行の内容は書き換えない（`applied_at` と `apply_error` を除く）
- **マイグレーションは何度実行しても安全であること。** `apply_migrations` は毎回すべての `.sql` を順に実行する
- **テストは実際の振る舞いを検証する。** モックの振る舞いを確認するだけのテストを書かない
- 枠は `じっくり`（利確 +22% / 損切り -8% / 期限なし / 2枠）と `回転`（利確 +10% / 損切り -5% / 期限 10営業日 / 2枠）の2つ。定義は `src/investment/config.py` の `BUCKETS` が唯一の出所
- 1銘柄に投じる上限は総資金の 25%、1取引で許容する損失は総資金の 2%、日本株は 100株単位
- ruff の設定に従う。暗黙の文字列連結（ISC004）を避け、複数行の文字列は括弧で囲む
- コミットメッセージは日本語。末尾に `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` を付ける

## 実行環境

```bash
export PATH="$HOME/.local/bin:$PATH"     # uv が PATH に無い場合
uv run pytest -q                          # テスト
uv run ruff check .                       # 静的検査
```

データベースを使うテスト（`@pytest.mark.integration`）は環境変数 `DATABASE_URL_TEST` が必要。
**本番の Neon には絶対に接続しないこと。** 使い捨てのローカル PostgreSQL を立てる:

```bash
docker run -d --rm --name invest-test-pg \
  -e POSTGRES_PASSWORD=test -e POSTGRES_DB=investtest -p 55432:5432 postgres:18
export DATABASE_URL_TEST="postgresql://postgres:test@localhost:55432/investtest"
```

**`.env` を作らないこと。** **`git push` を行わないこと。**

## ファイル構成

| ファイル | 責任 |
|---|---|
| `migrations/003_fills_and_push.sql`（新規） | `fills` / `push_subscriptions` テーブル、`trades` への列追加 |
| `src/investment/db.py`（変更） | 上記テーブルへの読み書き、総資金の計算、枠ごとの成績 |
| `src/investment/config.py`（変更） | `total_capital` を `initial_capital` に改名（初期入金額の意味に限定する） |
| `src/investment/fills.py`（新規） | 申告された約定を保有・現金・履歴に反映する計算。**外部依存を持たない純粋な計算だけ** |
| `src/investment/jobs/apply_fills.py`（新規） | 未反映の `fills` を読んで反映するジョブ |
| `src/investment/notify.py`（新規） | Web Push の送信 |
| `src/investment/jobs/build_context.py`（変更） | 総資金をDBから取る |
| `src/investment/jobs/apply_decision.py`（変更） | 総資金をDBから取る。採用があれば通知する |
| `src/investment/jobs/morning_check.py`（変更） | 到達・期限切れがあれば通知する |
| `.github/workflows/analyze.yml`（変更） | 反映ジョブを先に走らせる。通知用の鍵を渡す |
| `.github/workflows/morning.yml`（変更） | 同上 |

---

## Task 1: 総資金をデータベースから計算する

**なぜ:** `config.py` に `total_capital=550_000` が直書きされており、`cash` テーブルに実際の残高があるのにそちらを見ていない。記録が動き出すと現金が変動するため、直さないと初日から数字がずれ始め、しかも気づけない。いま直せば「550,000円と一致する」ことで正しさを確認できる。

**総資金の定義:** **現金の残高 ＋ 保有している株の取得原価（`quantity × avg_price`）** とする。
現在の株価は使わない。理由は2つ。

1. 現在値を使うと、含み益が出ているだけで次に買う金額が膨らみ、リスクが勝手に増える
2. 現在値の取得は通信が必要で、失敗しうる。総資金の計算が通信の失敗で止まるのは筋が悪い

利確して現金が増えれば総資金は増える。これで「利確して資金を増やし、さらに投資する」という循環は成立する。

**Files:**
- Modify: `src/investment/db.py`
- Modify: `src/investment/config.py`
- Modify: `src/investment/jobs/build_context.py`
- Modify: `src/investment/jobs/apply_decision.py`
- Modify: `src/investment/jobs/init_portfolio.py`
- Test: `tests/test_db.py`, `tests/test_config.py`, `tests/test_build_context.py`, `tests/test_apply_decision.py`

**Interfaces:**
- Produces: `db.select_capital(conn) -> float`
- Produces: `config.Settings.initial_capital`（`total_capital` から改名）
- Produces: `build_context.read_inputs(conn, criteria, limit) -> tuple[list[dict], list[dict], dict[str, float], float]`（4番目が総資金）
- Produces: `build_context.assemble(candidates, positions, cash, settings, criteria, dropped, capital) -> dict`
- Produces: `apply_decision.validate(decision, ctx, settings, capital, taken=None) -> list[str]`
- Produces: `apply_decision.process(conn, ctx, decisions, journal_path, settings, capital) -> tuple[int, int]`

- [ ] **Step 1: 総資金を計算するテストを書く**

`tests/test_db.py` の末尾に追加する。

```python
def test_select_capital_is_cash_plus_the_cost_of_what_we_hold(conn):
    """総資金 = 現金 + 保有の取得原価。

    現在の株価は使わない。含み益で次に買う金額が膨らむとリスクが勝手に増えるし、
    通信の失敗で総資金の計算が止まるのも筋が悪い。
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('JPY', 300000)")
        cur.execute(
            """
            INSERT INTO positions
                (symbol, quantity, avg_price, currency, take_profit, stop_loss,
                 opened_at, bucket)
            VALUES ('1111.T', 100, 1200, 'JPY', 1464, 1104, NOW(), 'じっくり')
            """
        )
    conn.commit()

    # 現金 300,000 + 100株 × 1,200円 = 420,000
    assert select_capital(conn) == 420000.0


def test_select_capital_with_no_positions_is_just_the_cash(conn):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('JPY', 550000)")
    conn.commit()

    assert select_capital(conn) == 550000.0


def test_select_capital_is_zero_when_nothing_has_been_deposited(conn):
    """入金前でも例外を投げず 0 を返す。呼び出し側で「買えない」と判断できる。"""
    assert select_capital(conn) == 0.0


def test_select_capital_ignores_currencies_other_than_yen(conn):
    """いまは日本株だけを扱う。ドルを円に足すと桁が狂うので数えない。

    米国株を有効にするときに、為替を掛けて足す形へ直す。
    """
    with conn.cursor() as cur:
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('JPY', 100000)")
        cur.execute("INSERT INTO cash (currency, amount) VALUES ('USD', 5000)")
    conn.commit()

    assert select_capital(conn) == 100000.0
```

`tests/test_db.py` の import に `select_capital` を加える。

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL（`ImportError: cannot import name 'select_capital'`）

- [ ] **Step 3: `select_capital` を実装する**

`src/investment/db.py` の `select_cash` の下に追加する。

```python
def select_capital(conn) -> float:
    """いまの総資金を返す。現金の残高 ＋ 保有の取得原価。

    現在の株価は使わない。理由は2つ。

    1. 現在値を使うと、含み益が出ているだけで次に買う金額が膨らみ、
       リスクが勝手に増えてしまう
    2. 現在値の取得は通信が必要で失敗しうる。総資金の計算が
       通信の失敗で止まるのは筋が悪い

    利確して現金が増えれば総資金も増えるので、
    「利確して資金を増やし、さらに投資する」という循環は成立する。

    いまは円だけを数える。ドルを円に足すと桁が狂うため。
    米国株を有効にするときに、為替を掛けて足す形へ直すこと。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT COALESCE(SUM(amount), 0) AS c FROM cash WHERE currency = 'JPY'")
        cash = float(cur.fetchone()["c"])
        cur.execute(
            """
            SELECT COALESCE(SUM(quantity * avg_price), 0) AS c
            FROM positions WHERE currency = 'JPY'
            """
        )
        held = float(cur.fetchone()["c"])
    return cash + held
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: PASS

- [ ] **Step 5: `total_capital` を `initial_capital` に改名する**

`src/investment/config.py`:

```python
@dataclass(frozen=True)
class Settings:
    initial_capital: int        # 最初に入金する額（円）。運用中の総資金はDBから計算する
    max_position_pct: float     # 1銘柄に投じる上限（資金に対する比率）
    risk_per_trade_pct: float   # 1取引で許容する損失（資金に対する比率）
    jp_fee_rate: float          # 日本株の往復手数料率
    us_fee_rate: float          # 米国株の往復手数料率
```

```python
SETTINGS = Settings(
    initial_capital=550_000,
    max_position_pct=0.25,
    risk_per_trade_pct=0.02,
    jp_fee_rate=0.0,
    us_fee_rate=0.01,
)
```

`Settings` のクラス直上に、なぜ改名したかのコメントを置く。

```python
# initial_capital は「最初に入金する額」であって、運用中の総資金ではない。
# 運用中の総資金は db.select_capital() が現金と保有から計算する。
# 以前はここに total_capital という名前で直書きしており、利確して資金が
# 増えても仕組みが気づけなかった（2026-09-07 に判明）。
```

`tests/test_config.py` の該当行を直す。

```python
    assert SETTINGS.initial_capital == 550_000
```

- [ ] **Step 6: `init_portfolio` を直す**

`src/investment/jobs/init_portfolio.py` の `SETTINGS.total_capital` を `SETTINGS.initial_capital` に置き換える。docstring の記述も合わせる。

Run: `uv run pytest tests/test_init_portfolio.py -q`
Expected: PASS

- [ ] **Step 7: `build_context` が総資金をDBから取るテストを書く**

`tests/test_build_context.py` に追加する。

```python
def test_read_inputs_returns_the_capital_calculated_from_the_database():
    """総資金をコードの直書きではなくデータベースから取ること。"""
    with (
        patch("investment.jobs.build_context.select_screened", return_value=[]),
        patch("investment.jobs.build_context.select_positions", return_value=[]),
        patch("investment.jobs.build_context.select_cash", return_value={"JPY": 620_000}),
        patch("investment.jobs.build_context.select_capital", return_value=620_000.0),
    ):
        candidates, positions, cash, capital = read_inputs(
            conn=None, criteria=SCREEN, limit=50
        )

    assert capital == 620_000.0


def test_assemble_reports_the_capital_it_was_given_not_the_initial_deposit():
    """コンテキストに入る総資金は、いまの額であること。"""
    ctx = assemble([], [], {"JPY": 620_000}, SETTINGS, SCREEN, dropped=[], capital=620_000.0)
    assert ctx["constraints"]["total_capital"] == 620_000.0


def test_the_buying_limit_grows_with_the_capital():
    """資金が増えたら、1銘柄に投じられる額も増えること。

    これが「利確して資金を増やし、さらに投資する」の中身である。
    550,000円のときは137,500円まで、620,000円なら155,000円まで買える。
    """
    kept, _ = drop_unaffordable(
        [{"symbol": "1111.T", "last_price": 1500.0}], SETTINGS, capital=620_000.0
    )
    assert len(kept) == 1                       # 100株で150,000円。155,000円以内
    assert kept[0]["max_quantity"] == 100

    kept, dropped = drop_unaffordable(
        [{"symbol": "1111.T", "last_price": 1500.0}], SETTINGS, capital=550_000.0
    )
    assert kept == []                           # 137,500円では買えない
    assert dropped[0]["symbol"] == "1111.T"
```

- [ ] **Step 8: テストが失敗することを確認する**

Run: `uv run pytest tests/test_build_context.py -q`
Expected: FAIL（引数の数が合わない）

- [ ] **Step 9: `build_context` を直す**

`src/investment/jobs/build_context.py`:

```python
from investment.db import (
    connect,
    record_gaps,
    select_capital,
    select_cash,
    select_positions,
    select_screened,
)
```

```python
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
```

`drop_unaffordable` / `_max_quantity` / `_max_entry_price` / `assemble` に `capital` を通す。

```python
def drop_unaffordable(
    candidates: list[dict], settings: Settings, capital: float
) -> tuple[list[dict], list[dict]]:
```

本体の `limit = settings.total_capital * settings.max_position_pct` を
`limit = capital * settings.max_position_pct` に変える。

```python
def _max_quantity(candidate: dict, settings: Settings, unit: int, capital: float) -> int:
```

本体の `settings.total_capital` を `capital` に変える。

```python
def assemble(
    candidates: list[dict],
    positions: list[dict],
    cash: dict[str, float],
    settings: Settings,
    criteria: ScreenCriteria,
    dropped: list[dict],
    capital: float,
) -> dict:
```

本体の `limit` と `"total_capital": settings.total_capital` を `capital` に変える。

`main()`:

```python
    with connect() as conn:
        candidates, positions, cash, capital = read_inputs(conn, SCREEN, limit=50)

    priced_candidates, gaps = attach_last_price(candidates)
    priced_candidates, dropped = drop_unaffordable(priced_candidates, SETTINGS, capital)
    ...
    ctx = assemble(priced_candidates, positions, cash, SETTINGS, SCREEN, dropped, capital)
    print(
        f"候補 {len(ctx['candidates'])} 件 / 保有 {len(ctx['positions'])} 件 / "
        f"総資金 {capital:,.0f}円 → {OUTPUT}"
    )
```

既存テストの `assemble(...)` / `drop_unaffordable(...)` 呼び出しに `capital=550_000.0` を足す。

- [ ] **Step 10: テストが通ることを確認する**

Run: `uv run pytest tests/test_build_context.py -q`
Expected: PASS

- [ ] **Step 11: `apply_decision` が総資金をDBから取るテストを書く**

`tests/test_apply_decision.py` に追加する。

```python
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
```

- [ ] **Step 12: テストが失敗することを確認する**

Run: `uv run pytest tests/test_apply_decision.py -q`
Expected: FAIL（`validate()` に `capital` 引数が無い）

- [ ] **Step 13: `apply_decision` を直す**

`src/investment/jobs/apply_decision.py`:

```python
def validate(
    decision: dict,
    ctx: dict,
    settings: Settings,
    capital: float,
    taken: dict[str, int] | None = None,
) -> list[str]:
```

本体の `settings.total_capital` を全て `capital` に置き換える（`position_size` の呼び出し、
`cap_amount`、`risk_amount`、却下メッセージ）。

```python
def process(
    conn,
    ctx: dict,
    decisions: list[dict],
    journal_path: str,
    settings: Settings,
    capital: float,
) -> tuple[int, int]:
```

本体の `validate(d, ctx, settings, taken)` を `validate(d, ctx, settings, capital, taken)` に変える。

`enrich` が `settings` を使っている場合は `capital` も渡す（必要勝率の計算に総資金は使わないので、
使っていなければ変更不要）。

`main()`:

```python
    with connect() as conn:
        if missing is not None:
            record_gap(conn, scope=missing[0], detail=missing[1])
            print(missing[1])
            return 1
        capital = select_capital(conn)
        print(f"総資金 {capital:,.0f}円 で検証します")
        process(conn, ctx, decisions, journal_path, SETTINGS, capital)
```

import に `select_capital` を加える。既存テストの `validate(...)` / `process(...)` 呼び出しに
`capital=550_000.0` を足す。

- [ ] **Step 14: 全テストと静的検査を通す**

Run: `uv run ruff check . && uv run pytest -q`
Expected: 全て PASS

- [ ] **Step 15: 本番と同じ値になることを確認する**

`select_capital` が現状（現金550,000円・保有0件）で 550,000.0 を返すことを、
テスト用データベースで確認する。

```bash
export DATABASE_URL_TEST="postgresql://postgres:test@localhost:55432/investtest"
uv run python - <<'EOF'
import os
from investment.db import apply_migrations, connect, init_cash, select_capital
with connect(os.environ["DATABASE_URL_TEST"]) as conn:
    apply_migrations(conn)
    init_cash(conn, jpy=550_000)
    print("総資金:", select_capital(conn))
EOF
```

Expected: `総資金: 550000.0`

- [ ] **Step 16: コミット**

```bash
git add -A
git commit -m "fix: 総資金を直書きからデータベースの実残高に変える

config.py の total_capital=550_000 が直書きで、cash テーブルに実際の
残高があるのにそちらを見ていなかった。記録が動き出すと現金が変動するため、
直さないと初日から数字がずれ始め、しかも気づけない。

総資金 = 現金の残高 + 保有の取得原価 とした。現在の株価を使わないのは、
含み益で次に買う金額が膨らむとリスクが勝手に増えることと、通信の失敗で
総資金の計算が止まるのを避けるため。利確して現金が増えれば総資金も増えるので、
「利確して資金を増やし、さらに投資する」という循環は成立する。

config の total_capital は initial_capital に改名した。あれは
「最初に入金する額」であって運用中の総資金ではない。

いまの本番の状態（現金550,000円・保有0件）で 550,000.0 になることを確認済み。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 2: 記録用のテーブルを作る

**Files:**
- Create: `migrations/003_fills_and_push.sql`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: テーブル `fills`、テーブル `push_subscriptions`、`trades` の列 `bucket` / `realized_pnl` / `holding_days`

**なぜ `trades` に3つ列を足すのか:** 枠ごとの成績（勝率・平均保有日数・損益）を出すため。
売った時点で「どの枠か」「いくら儲かったか」「何日持ったか」を記録しておけば、
あとで突き合わせ直す必要がない。突き合わせ直す方式にすると、
買いと売りの対応づけ（どの買いに対する売りか）という曖昧さが入り込む。

- [ ] **Step 1: マイグレーションのテストを書く**

`tests/test_db.py` に追加する。

```python
def test_fills_table_accepts_a_recorded_purchase(conn):
    """利用者が申告した約定を、そのまま1行入れられること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'buy', 100, 899, 'JPY')
            RETURNING id, applied_at, apply_error
            """
        )
        row = cur.fetchone()
    conn.commit()

    assert row["id"] > 0
    assert row["applied_at"] is None   # 入れた直後は未反映
    assert row["apply_error"] is None


def test_fills_table_rejects_a_side_that_is_neither_buy_nor_sell(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'なんとなく', 100, 899, 'JPY')
            """
        )


def test_fills_table_rejects_zero_or_negative_quantity(conn):
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'buy', 0, 899, 'JPY')
            """
        )


def test_trades_table_can_record_the_bucket_and_the_result(conn):
    """枠ごとの成績を出すために、売った時点の結果を取引に残せること。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency,
                 bucket, realized_pnl, holding_days)
            VALUES (NOW(), '1111.T', 'sell', 100, 1100, 'JPY', '回転', 20000, 9)
            RETURNING bucket, realized_pnl, holding_days
            """
        )
        row = cur.fetchone()
    conn.commit()

    assert row["bucket"] == "回転"
    assert float(row["realized_pnl"]) == 20000.0
    assert row["holding_days"] == 9


def test_push_subscriptions_are_unique_per_endpoint(conn):
    """同じ端末から2回登録しても、行が増えないこと。"""
    sql = """
        INSERT INTO push_subscriptions (endpoint, p256dh, auth)
        VALUES ('https://example.test/abc', 'k1', 'a1')
        ON CONFLICT (endpoint) DO UPDATE SET p256dh = EXCLUDED.p256dh
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(sql)
        cur.execute("SELECT COUNT(*) AS c FROM push_subscriptions")
        assert cur.fetchone()["c"] == 1
    conn.commit()
```

`tests/test_db.py` の `conn` フィクスチャの `TRUNCATE` に、新しいテーブルを加える。

```python
            cur.execute(
                "TRUNCATE fundamentals, trades, positions, proposals, cash, data_gaps, "
                "fills, push_subscriptions"
            )
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL（`relation "fills" does not exist`）

- [ ] **Step 3: マイグレーションを書く**

`migrations/003_fills_and_push.sql`:

```sql
-- 計画2: スマホから記録できるようにするために足すもの。
--
-- fills は「利用者がこう買った/売ったと申告した内容」をそのまま残す表。
-- 保有株数の計算や現金の増減は、この表を読んだ Python が行う。
-- 申告と計算結果を分けて持つことで、数字が合わないときに
-- 申告が違うのか計算が違うのかを切り分けられる。

CREATE TABLE IF NOT EXISTS fills (
    id           BIGSERIAL      PRIMARY KEY,
    recorded_at  TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    -- 買いは、どの提案に対する約定かを持つ。枠・利確・損切りをここから引く。
    -- 売りは提案を経由しないことがあるので NULL を許す。
    proposal_id  BIGINT         REFERENCES proposals(id),
    symbol       TEXT           NOT NULL,
    side         TEXT           NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity     INTEGER        NOT NULL CHECK (quantity > 0),
    price        NUMERIC(18, 4) NOT NULL CHECK (price > 0),
    currency     TEXT           NOT NULL,
    fee          NUMERIC(18, 4) NOT NULL DEFAULT 0,
    -- 反映済みなら日時が入る。NULL は未反映。
    applied_at   TIMESTAMPTZ,
    -- 反映できなかった理由。黙って消さないために残す。
    apply_error  TEXT
);

-- 未反映のものを探す問い合わせが毎回走るので、索引を作る。
CREATE INDEX IF NOT EXISTS fills_unapplied_idx ON fills (id) WHERE applied_at IS NULL;

-- 通知の宛先。endpoint が宛先そのもので、端末ごとに一意。
CREATE TABLE IF NOT EXISTS push_subscriptions (
    id            BIGSERIAL   PRIMARY KEY,
    endpoint      TEXT        NOT NULL UNIQUE,
    p256dh        TEXT        NOT NULL,
    auth          TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_ok_at    TIMESTAMPTZ,
    failure_count INTEGER     NOT NULL DEFAULT 0
);

-- 枠ごとの成績（勝率・平均保有日数・損益）を出すために、
-- 売った時点の結果を取引に残す。あとで買いと売りを突き合わせ直す方式にすると、
-- 「どの買いに対する売りか」という曖昧さが入り込む。
ALTER TABLE trades ADD COLUMN IF NOT EXISTS bucket       TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS realized_pnl NUMERIC(18, 4);
ALTER TABLE trades ADD COLUMN IF NOT EXISTS holding_days INTEGER;
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: PASS

- [ ] **Step 5: 何度実行しても安全なことを確認する**

```bash
uv run python - <<'EOF'
import os
from investment.db import apply_migrations, connect
with connect(os.environ["DATABASE_URL_TEST"]) as conn:
    for i in (1, 2, 3):
        apply_migrations(conn)
        print(f"{i}回目: 成功")
EOF
```

Expected: 3回とも成功

- [ ] **Step 6: コミット**

```bash
git add -A
git commit -m "feat: 記録用のテーブル（fills / push_subscriptions）を作る

fills は「利用者がこう買った/売ったと申告した内容」をそのまま残す表。
保有株数や現金の計算は、この表を読んだ Python が行う。申告と計算結果を
分けて持つことで、数字が合わないときに申告が違うのか計算が違うのかを
切り分けられる。

trades に bucket / realized_pnl / holding_days を足した。枠ごとの成績を
出すために、売った時点の結果を残す。あとで買いと売りを突き合わせ直す方式は
「どの買いに対する売りか」という曖昧さが入り込むため採らない。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 3: 買った記録を反映する計算

**なぜ計算だけを別ファイルにするか:** データベースの読み書きと計算が混ざっていると、
計算のテストにデータベースが要る。テストが遅く、書きにくくなる。
`src/investment/fills.py` は**外部依存を持たない純粋な計算**だけを置き、
データベースへの反映は Task 5 のジョブが行う。

**Files:**
- Create: `src/investment/fills.py`
- Test: `tests/test_fills.py`（新規）

**Interfaces:**
- Produces: `fills.Position`（データクラス。`symbol` / `quantity` / `avg_price` / `bucket` / `take_profit` / `stop_loss`）
- Produces: `fills.BuyResult`（データクラス。`position: Position` / `cash_delta: float` / `trade: dict`）
- Produces: `fills.apply_buy(existing, fill, rule, cash) -> BuyResult`
- Produces: `fills.FillError`（例外）

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_fills.py`（新規）:

```python
"""買った/売った の申告を、保有と現金に反映する計算のテスト。

データベースには触れない純粋な計算なので、ここでのテストは速い。
"""

import pytest

from investment.config import bucket_by_name
from investment.fills import FillError, Position, apply_buy

PATIENT = bucket_by_name("じっくり")   # 利確 +22% / 損切り -8%
FAST = bucket_by_name("回転")          # 利確 +10% / 損切り -5%


def _fill(**overrides) -> dict:
    d = {
        "symbol": "1111.T",
        "side": "buy",
        "quantity": 100,
        "price": 900.0,
        "currency": "JPY",
        "fee": 0.0,
    }
    d.update(overrides)
    return d


def test_buying_a_stock_we_do_not_hold_creates_a_position():
    result = apply_buy(existing=None, fill=_fill(), rule=PATIENT, cash=550_000.0)

    assert result.position.symbol == "1111.T"
    assert result.position.quantity == 100
    assert result.position.avg_price == 900.0
    assert result.position.bucket == "じっくり"
    # 利確・損切りは、買値に枠の率を掛けた値
    assert result.position.take_profit == pytest.approx(1098.0)   # 900 × 1.22
    assert result.position.stop_loss == pytest.approx(828.0)      # 900 × 0.92


def test_buying_reduces_the_cash_by_the_cost_including_the_fee():
    result = apply_buy(existing=None, fill=_fill(fee=500.0), rule=PATIENT, cash=550_000.0)

    # 100株 × 900円 + 手数料500円 = 90,500円
    assert result.cash_delta == pytest.approx(-90_500.0)


def test_buying_records_a_trade_with_the_bucket():
    """あとで枠ごとの成績を出すために、取引に枠を残す。"""
    result = apply_buy(existing=None, fill=_fill(), rule=PATIENT, cash=550_000.0)

    assert result.trade["side"] == "buy"
    assert result.trade["symbol"] == "1111.T"
    assert result.trade["quantity"] == 100
    assert result.trade["price"] == 900.0
    assert result.trade["bucket"] == "じっくり"
    # 買った時点では損益も保有日数も決まらない
    assert result.trade["realized_pnl"] is None
    assert result.trade["holding_days"] is None


def test_buying_more_of_the_same_stock_averages_the_price():
    """買い増したら、平均取得単価を計算し直すこと。

    100株を900円で持っているところに、100株を1,100円で買い増すと、
    200株の平均は1,000円になる。
    """
    existing = Position(
        symbol="1111.T", quantity=100, avg_price=900.0, bucket="じっくり",
        take_profit=1098.0, stop_loss=828.0,
    )
    result = apply_buy(
        existing=existing, fill=_fill(price=1100.0), rule=PATIENT, cash=550_000.0
    )

    assert result.position.quantity == 200
    assert result.position.avg_price == pytest.approx(1000.0)
    # 利確・損切りも、新しい平均から計算し直す
    assert result.position.take_profit == pytest.approx(1220.0)   # 1000 × 1.22
    assert result.position.stop_loss == pytest.approx(920.0)      # 1000 × 0.92


def test_buying_more_in_a_different_bucket_is_rejected():
    """同じ銘柄を違う枠で持つことはできない。

    枠ごとに成績を測っているので、1つの保有が2つの枠にまたがると
    どちらの成績なのか決められなくなる。
    """
    existing = Position(
        symbol="1111.T", quantity=100, avg_price=900.0, bucket="じっくり",
        take_profit=1098.0, stop_loss=828.0,
    )
    with pytest.raises(FillError) as exc:
        apply_buy(existing=existing, fill=_fill(), rule=FAST, cash=550_000.0)

    assert "枠" in str(exc.value)


def test_buying_more_than_the_cash_allows_is_rejected():
    """現金が足りない買いは受け付けない。

    仮想資金の段階でも、現金がマイナスになると収支が意味を失う。
    """
    with pytest.raises(FillError) as exc:
        apply_buy(existing=None, fill=_fill(quantity=1000), rule=PATIENT, cash=50_000.0)

    assert "現金" in str(exc.value)
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_fills.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'investment.fills'`）

- [ ] **Step 3: `fills.py` を実装する**

`src/investment/fills.py`（新規）:

```python
"""利用者が申告した約定を、保有と現金にどう反映するかの計算。

外部に一切依存しない純粋な計算だけを置く。データベースへの書き込みは
investment.jobs.apply_fills が行う。分けている理由は、計算のテストに
データベースを用意しなくて済むようにするため。

**この計算は Python にしか無い。** 画面側（JavaScript）に同じ計算を書くと、
同じことが2箇所に存在して片方だけ直っていない状態が生まれる。
"""

from dataclasses import dataclass

from investment.config import Bucket


class FillError(RuntimeError):
    """申告された約定を反映できない。

    利用者の入力ミス（持っていない銘柄を売った、現金が足りない等）で起きる。
    黙って無視せず、理由を残して利用者に見せる。
    """


@dataclass(frozen=True)
class Position:
    """1銘柄の保有。"""

    symbol: str
    quantity: int
    avg_price: float
    bucket: str
    take_profit: float
    stop_loss: float


@dataclass(frozen=True)
class BuyResult:
    """買いを反映した結果。

    cash_delta は現金の増減（買いなので負の数）。
    trade はそのまま trades テーブルに入れる内容。
    """

    position: Position
    cash_delta: float
    trade: dict


def _exits(price: float, rule: Bucket) -> tuple[float, float]:
    """買値から、利確と損切りの価格を計算する。"""
    return price * (1 + rule.take_profit_pct), price * (1 - rule.stop_loss_pct)


def apply_buy(
    existing: Position | None, fill: dict, rule: Bucket, cash: float
) -> BuyResult:
    """買った申告を反映した結果を返す。

    existing はいまの保有（無ければ None）。rule は買った枠。
    cash は現在の現金残高で、足りるかの確認に使う。
    """
    cost = fill["quantity"] * fill["price"] + fill["fee"]
    if cost > cash:
        raise FillError(
            f"現金が足りません（必要 {cost:,.0f}円 / 残高 {cash:,.0f}円）"
        )

    if existing is None:
        quantity = fill["quantity"]
        avg_price = fill["price"]
    else:
        if existing.bucket != rule.name:
            raise FillError(
                f"{fill['symbol']} は既に{existing.bucket}枠で持っています。"
                f"同じ銘柄を{rule.name}枠でも持つことはできません"
                f"（どちらの枠の成績か決められなくなるため）"
            )
        quantity = existing.quantity + fill["quantity"]
        total_cost = existing.quantity * existing.avg_price + fill["quantity"] * fill["price"]
        avg_price = total_cost / quantity

    take_profit, stop_loss = _exits(avg_price, rule)

    return BuyResult(
        position=Position(
            symbol=fill["symbol"],
            quantity=quantity,
            avg_price=avg_price,
            bucket=rule.name,
            take_profit=take_profit,
            stop_loss=stop_loss,
        ),
        cash_delta=-cost,
        trade={
            "symbol": fill["symbol"],
            "side": "buy",
            "quantity": fill["quantity"],
            "price": fill["price"],
            "currency": fill["currency"],
            "fee": fill["fee"],
            "bucket": rule.name,
            "realized_pnl": None,
            "holding_days": None,
        },
    )
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_fills.py -q`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add -A
git commit -m "feat: 買った記録を保有と現金に反映する計算

データベースに触れない純粋な計算として investment/fills.py に置く。
テストにデータベースを用意しなくて済むようにするため。

買い増したときは平均取得単価を計算し直し、利確・損切りも新しい平均から
引き直す。同じ銘柄を違う枠で持つことは拒否する（1つの保有が2つの枠に
またがると、どちらの枠の成績か決められなくなるため）。現金が足りない
買いも拒否する。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 4: 売った記録を反映する計算

**Files:**
- Modify: `src/investment/fills.py`
- Test: `tests/test_fills.py`

**Interfaces:**
- Consumes: `fills.Position`, `fills.FillError`
- Produces: `fills.SellResult`（`position: Position | None` / `cash_delta: float` / `trade: dict`）
- Produces: `fills.apply_sell(existing, fill, opened_at, today) -> SellResult`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_fills.py` に追加する。

```python
from datetime import date

from investment.fills import apply_sell


def _held(**overrides) -> Position:
    d = {
        "symbol": "1111.T", "quantity": 100, "avg_price": 900.0,
        "bucket": "じっくり", "take_profit": 1098.0, "stop_loss": 828.0,
    }
    d.update(overrides)
    return Position(**d)


def test_selling_everything_removes_the_position():
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=1100.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    assert result.position is None      # 全部売ったので保有は消える


def test_selling_everything_adds_the_proceeds_to_the_cash():
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=1100.0, fee=300.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    # 100株 × 1,100円 - 手数料300円 = 109,700円
    assert result.cash_delta == pytest.approx(109_700.0)


def test_selling_records_the_profit_the_bucket_and_the_days_held():
    """枠ごとの成績を出すのに必要な3つを、売った時点で確定させる。"""
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=1100.0, fee=300.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    # (売値1,100円 - 平均取得900円) × 100株 - 手数料300円 = 19,700円
    assert result.trade["realized_pnl"] == pytest.approx(19_700.0)
    assert result.trade["bucket"] == "じっくり"
    assert result.trade["holding_days"] == 29
    assert result.trade["side"] == "sell"


def test_selling_at_a_loss_records_a_negative_profit():
    result = apply_sell(
        existing=_held(),
        fill=_fill(side="sell", quantity=100, price=828.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 10),
    )

    # (828 - 900) × 100 = -7,200円
    assert result.trade["realized_pnl"] == pytest.approx(-7_200.0)


def test_selling_part_of_a_position_keeps_the_rest_at_the_same_average():
    """一部だけ売ったら、残りの平均取得単価は変わらない。

    平均取得単価は「いくらで買ったか」なので、売っても動かない。
    """
    result = apply_sell(
        existing=_held(quantity=300),
        fill=_fill(side="sell", quantity=100, price=1100.0),
        opened_at=date(2026, 9, 1),
        today=date(2026, 9, 30),
    )

    assert result.position is not None
    assert result.position.quantity == 200
    assert result.position.avg_price == pytest.approx(900.0)
    assert result.position.bucket == "じっくり"


def test_selling_more_than_we_hold_is_rejected():
    with pytest.raises(FillError) as exc:
        apply_sell(
            existing=_held(quantity=100),
            fill=_fill(side="sell", quantity=200, price=1100.0),
            opened_at=date(2026, 9, 1),
            today=date(2026, 9, 30),
        )

    assert "保有" in str(exc.value)


def test_selling_a_stock_we_do_not_hold_is_rejected():
    with pytest.raises(FillError) as exc:
        apply_sell(
            existing=None,
            fill=_fill(side="sell", quantity=100, price=1100.0),
            opened_at=None,
            today=date(2026, 9, 30),
        )

    assert "保有していません" in str(exc.value)
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_fills.py -q`
Expected: FAIL（`cannot import name 'apply_sell'`）

- [ ] **Step 3: `apply_sell` を実装する**

`src/investment/fills.py` に追加する（import に `from datetime import date` を加える）。

```python
@dataclass(frozen=True)
class SellResult:
    """売りを反映した結果。

    position が None なら、全部売って保有が無くなったという意味。
    cash_delta は現金の増減（売りなので正の数）。
    """

    position: Position | None
    cash_delta: float
    trade: dict


def apply_sell(
    existing: Position | None, fill: dict, opened_at: date | None, today: date
) -> SellResult:
    """売った申告を反映した結果を返す。

    opened_at はその銘柄を最初に持った日。保有日数の計算に使う。

    確定した損益・枠・保有日数を、この時点で取引に書き込む。あとで買いと
    売りを突き合わせ直す方式にすると「どの買いに対する売りか」という
    曖昧さが入り込むため、売った時点で確定させる。
    """
    if existing is None:
        raise FillError(f"{fill['symbol']} を保有していません")
    if fill["quantity"] > existing.quantity:
        raise FillError(
            f"{fill['symbol']} の保有は {existing.quantity} 株で、"
            f"{fill['quantity']} 株は売れません"
        )

    proceeds = fill["quantity"] * fill["price"] - fill["fee"]
    realized = (fill["price"] - existing.avg_price) * fill["quantity"] - fill["fee"]
    remaining = existing.quantity - fill["quantity"]

    # 平均取得単価は「いくらで買ったか」なので、売っても動かさない。
    position = None
    if remaining > 0:
        position = Position(
            symbol=existing.symbol,
            quantity=remaining,
            avg_price=existing.avg_price,
            bucket=existing.bucket,
            take_profit=existing.take_profit,
            stop_loss=existing.stop_loss,
        )

    holding_days = (today - opened_at).days if opened_at is not None else None

    return SellResult(
        position=position,
        cash_delta=proceeds,
        trade={
            "symbol": fill["symbol"],
            "side": "sell",
            "quantity": fill["quantity"],
            "price": fill["price"],
            "currency": fill["currency"],
            "fee": fill["fee"],
            "bucket": existing.bucket,
            "realized_pnl": realized,
            "holding_days": holding_days,
        },
    )
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_fills.py -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add -A
git commit -m "feat: 売った記録を保有と現金に反映する計算

確定した損益・枠・保有日数を、売った時点で取引に書き込む。あとで買いと
売りを突き合わせ直す方式は「どの買いに対する売りか」という曖昧さが
入り込むため採らない。

一部だけ売った場合、残りの平均取得単価は変えない（平均取得単価は
いくらで買ったかを表す値なので、売っても動かない）。
保有を超える売りと、持っていない銘柄の売りは拒否する。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 5: 反映するジョブ

**Files:**
- Create: `src/investment/jobs/apply_fills.py`
- Modify: `src/investment/db.py`
- Test: `tests/test_apply_fills.py`（新規）

**Interfaces:**
- Consumes: `fills.apply_buy`, `fills.apply_sell`, `fills.Position`, `fills.FillError`
- Produces: `db.select_unapplied_fills(conn) -> list[dict]`
- Produces: `db.mark_fill_applied(conn, fill_id) -> None`
- Produces: `db.mark_fill_failed(conn, fill_id, reason) -> None`
- Produces: `db.save_fill_result(conn, fill_id, position, cash_delta, trade, currency) -> None`
- Produces: `apply_fills.run(conn, today) -> tuple[int, int]`（反映できた件数, できなかった件数）

- [ ] **Step 1: データベース関数のテストを書く**

`tests/test_db.py` に追加する。

```python
def test_select_unapplied_fills_returns_only_the_ones_not_yet_applied(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency, applied_at)
            VALUES ('1111.T', 'buy', 100, 900, 'JPY', NOW()),
                   ('2222.T', 'buy', 100, 800, 'JPY', NULL),
                   ('3333.T', 'sell', 100, 700, 'JPY', NULL)
            """
        )
    conn.commit()

    rows = select_unapplied_fills(conn)

    assert [r["symbol"] for r in rows] == ["2222.T", "3333.T"]   # 古い順


def test_mark_fill_failed_keeps_the_row_and_records_the_reason(conn):
    """反映できなかった記録を消さないこと。理由を残して利用者に見せる。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'sell', 100, 900, 'JPY') RETURNING id
            """
        )
        fill_id = cur.fetchone()["id"]
    conn.commit()

    mark_fill_failed(conn, fill_id, "1111.T を保有していません")

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fill_id,))
        row = cur.fetchone()
    assert row["applied_at"] is None                      # 未反映のまま
    assert "保有していません" in row["apply_error"]
```

`tests/test_db.py` の import に `mark_fill_failed`, `select_unapplied_fills` を加える。

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL（import できない）

- [ ] **Step 3: データベース関数を実装する**

`src/investment/db.py` に追加する。

```python
def select_unapplied_fills(conn) -> list[dict]:
    """まだ保有・現金に反映していない申告を、古い順に返す。

    古い順に処理しないと、買う前に売ることになって反映に失敗する。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM fills WHERE applied_at IS NULL ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def mark_fill_failed(conn, fill_id: int, reason: str) -> None:
    """反映できなかった理由を記録する。行は消さず、未反映のまま残す。

    消してしまうと、利用者は「記録したはずなのに無い」という状態に置かれ、
    原因も分からなくなる。
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fills SET apply_error = %s WHERE id = %s", (reason, fill_id)
        )
    conn.commit()


def save_fill_result(
    conn,
    fill_id: int,
    position,
    cash_delta: float,
    trade: dict,
    currency: str,
) -> None:
    """1件の申告の反映を、まとめて1つのトランザクションで書き込む。

    取引の追加・保有の更新・現金の増減・申告を反映済みにする、の4つは
    途中で止まると帳尻が合わなくなるため、必ず全部成功か全部取り消しにする。

    position が None なら、その銘柄の保有を削除する（全部売った場合）。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency, fee,
                 bucket, realized_pnl, holding_days)
            VALUES (NOW(), %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                trade["symbol"], trade["side"], trade["quantity"], trade["price"],
                trade["currency"], trade["fee"], trade["bucket"],
                trade["realized_pnl"], trade["holding_days"],
            ),
        )

        if position is None:
            cur.execute("DELETE FROM positions WHERE symbol = %s", (trade["symbol"],))
        else:
            cur.execute(
                """
                INSERT INTO positions
                    (symbol, quantity, avg_price, currency, take_profit, stop_loss,
                     opened_at, bucket)
                VALUES (%s, %s, %s, %s, %s, %s, NOW(), %s)
                ON CONFLICT (symbol) DO UPDATE SET
                    quantity    = EXCLUDED.quantity,
                    avg_price   = EXCLUDED.avg_price,
                    take_profit = EXCLUDED.take_profit,
                    stop_loss   = EXCLUDED.stop_loss,
                    bucket      = EXCLUDED.bucket
                """,
                (
                    position.symbol, position.quantity, position.avg_price, currency,
                    position.take_profit, position.stop_loss, position.bucket,
                ),
            )

        cur.execute(
            "UPDATE cash SET amount = amount + %s WHERE currency = %s",
            (cash_delta, currency),
        )
        cur.execute(
            "UPDATE fills SET applied_at = NOW(), apply_error = NULL WHERE id = %s",
            (fill_id,),
        )
    conn.commit()
```

**注意:** `opened_at` は `ON CONFLICT` の更新対象に入れない。買い増しても
「最初に持った日」を動かさないため（回転枠の期限がリセットされてしまう）。

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: PASS

- [ ] **Step 5: ジョブのテストを書く**

`tests/test_apply_fills.py`（新規）:

```python
"""申告を反映するジョブのテスト。実際のデータベースを使う。"""

import os
from datetime import date

import pytest

from investment.db import apply_migrations, connect, init_cash
from investment.jobs.apply_fills import run

pytestmark = pytest.mark.integration

TEST_URL = os.environ.get("DATABASE_URL_TEST")


@pytest.fixture
def conn():
    if not TEST_URL:
        pytest.skip("DATABASE_URL_TEST が未設定のためスキップします")
    with connect(TEST_URL) as c:
        apply_migrations(c)
        with c.cursor() as cur:
            cur.execute(
                "TRUNCATE fundamentals, trades, positions, proposals, cash, data_gaps, "
                "fills, push_subscriptions"
            )
        c.commit()
        init_cash(c, jpy=550_000)
        yield c


def _proposal(conn, symbol: str, bucket: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO proposals
                (created_at, symbol, action, quantity, entry_price, take_profit,
                 stop_loss, required_win_rate, rationale, scenario, confidence,
                 strategy_tag, bucket, rule_version, journal_path)
            VALUES (NOW(), %s, 'buy', 100, 900, 1098, 828, 0.2667,
                    'x', 'y', 'mid', 'z', %s, 'v3', 'journal/x.md')
            RETURNING id
            """,
            (symbol, bucket),
        )
        pid = cur.fetchone()["id"]
    conn.commit()
    return pid


def _fill(conn, **kw) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency, fee)
            VALUES (%(proposal_id)s, %(symbol)s, %(side)s, %(quantity)s,
                    %(price)s, %(currency)s, %(fee)s)
            RETURNING id
            """,
            {"proposal_id": None, "currency": "JPY", "fee": 0, **kw},
        )
        fid = cur.fetchone()["id"]
    conn.commit()
    return fid


def _state(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM positions ORDER BY symbol")
        positions = [dict(r) for r in cur.fetchall()]
        cur.execute("SELECT amount FROM cash WHERE currency = 'JPY'")
        cash = float(cur.fetchone()["amount"])
        cur.execute("SELECT * FROM trades ORDER BY id")
        trades = [dict(r) for r in cur.fetchall()]
    return {"positions": positions, "cash": cash, "trades": trades}


def test_applying_a_buy_creates_the_position_and_reduces_the_cash(conn):
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (1, 0)

    s = _state(conn)
    assert len(s["positions"]) == 1
    assert s["positions"][0]["symbol"] == "1111.T"
    assert s["positions"][0]["quantity"] == 100
    assert s["positions"][0]["bucket"] == "じっくり"
    assert s["cash"] == 550_000 - 90_000
    assert len(s["trades"]) == 1
    assert s["trades"][0]["bucket"] == "じっくり"


def test_applying_a_buy_marks_the_proposal_as_taken(conn):
    """提案どおりに買ったら、その提案を「実行した」にすること。

    pending のまま残ると、次の実行でも「まだ買っていない提案」として出てくる。
    """
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    run(conn, today=date(2026, 9, 8))

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM proposals WHERE id = %s", (pid,))
        assert cur.fetchone()["outcome"] == "taken"


def test_applying_a_sell_removes_the_position_and_adds_the_cash(conn):
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)
    run(conn, today=date(2026, 9, 8))

    _fill(conn, symbol="1111.T", side="sell", quantity=100, price=1100)
    assert run(conn, today=date(2026, 9, 30)) == (1, 0)

    s = _state(conn)
    assert s["positions"] == []
    assert s["cash"] == 550_000 - 90_000 + 110_000
    sell = s["trades"][-1]
    assert sell["side"] == "sell"
    assert float(sell["realized_pnl"]) == 20_000.0
    assert sell["bucket"] == "じっくり"


def test_a_fill_that_cannot_be_applied_is_kept_with_its_reason(conn):
    """反映できない申告を黙って消さないこと。"""
    fid = _fill(conn, symbol="9999.T", side="sell", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT applied_at, apply_error FROM fills WHERE id = %s", (fid,))
        row = cur.fetchone()
    assert row["applied_at"] is None
    assert "保有していません" in row["apply_error"]


def test_one_bad_fill_does_not_stop_the_others(conn):
    """1件の失敗で全体を止めないこと。"""
    _fill(conn, symbol="9999.T", side="sell", quantity=100, price=900)   # 失敗する
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (1, 1)
    assert len(_state(conn)["positions"]) == 1


def test_a_buy_without_a_proposal_is_rejected(conn):
    """買いは提案に紐づいていないと、どの枠か決められない。"""
    fid = _fill(conn, symbol="1111.T", side="buy", quantity=100, price=900)

    assert run(conn, today=date(2026, 9, 8)) == (0, 1)

    with conn.cursor() as cur:
        cur.execute("SELECT apply_error FROM fills WHERE id = %s", (fid,))
        assert "枠" in cur.fetchone()["apply_error"]


def test_applying_the_same_fill_twice_does_not_double_count(conn):
    """反映済みの申告を二度反映しないこと。"""
    pid = _proposal(conn, "1111.T", "じっくり")
    _fill(conn, proposal_id=pid, symbol="1111.T", side="buy", quantity=100, price=900)

    run(conn, today=date(2026, 9, 8))
    assert run(conn, today=date(2026, 9, 8)) == (0, 0)   # 2回目は対象なし

    s = _state(conn)
    assert s["positions"][0]["quantity"] == 100
    assert len(s["trades"]) == 1
```

- [ ] **Step 6: テストが失敗することを確認する**

Run: `uv run pytest tests/test_apply_fills.py -q`
Expected: FAIL（`No module named 'investment.jobs.apply_fills'`）

- [ ] **Step 7: ジョブを実装する**

`src/investment/jobs/apply_fills.py`（新規）:

```python
"""利用者が申告した約定を、保有・現金・取引履歴に反映する。

スマホの画面から「買った」を押すと、その内容が fills テーブルに
1行そのまま入る。画面側は計算をしない。計算はこのジョブだけが行う。

同じ計算を画面側（JavaScript）にも書くと、同じことが2箇所に存在して
片方だけ直っていない状態が生まれる。それを避けるための分担である。
"""

import sys
from datetime import date, datetime
from zoneinfo import ZoneInfo

from investment.config import bucket_by_name
from investment.db import (
    connect,
    mark_fill_failed,
    save_fill_result,
    select_capital,
    select_cash,
    select_positions,
    select_unapplied_fills,
)
from investment.fills import FillError, Position, apply_buy, apply_sell

JST = ZoneInfo("Asia/Tokyo")


def _position_of(conn, symbol: str) -> tuple[Position | None, date | None]:
    """いまの保有と、最初に持った日を返す。"""
    for p in select_positions(conn):
        if p["symbol"] == symbol:
            opened = p["opened_at"]
            return (
                Position(
                    symbol=p["symbol"],
                    quantity=int(p["quantity"]),
                    avg_price=float(p["avg_price"]),
                    bucket=p["bucket"],
                    take_profit=float(p["take_profit"]),
                    stop_loss=float(p["stop_loss"]),
                ),
                opened.astimezone(JST).date() if opened is not None else None,
            )
    return None, None


def _bucket_of_proposal(conn, proposal_id) -> str | None:
    if proposal_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT bucket FROM proposals WHERE id = %s", (proposal_id,))
        row = cur.fetchone()
    return row["bucket"] if row else None


def _mark_proposal_taken(conn, proposal_id) -> None:
    if proposal_id is None:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE proposals SET outcome = 'taken' WHERE id = %s", (proposal_id,)
        )
    conn.commit()


def run(conn, today: date) -> tuple[int, int]:
    """未反映の申告を古い順に反映する。戻り値は (反映できた件数, できなかった件数)。

    1件の失敗で全体を止めない。失敗した申告は消さず、理由を残す。
    """
    ok = failed = 0
    for row in select_unapplied_fills(conn):
        fill = {
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": int(row["quantity"]),
            "price": float(row["price"]),
            "currency": row["currency"],
            "fee": float(row["fee"]),
        }
        existing, opened_at = _position_of(conn, fill["symbol"])

        try:
            if fill["side"] == "buy":
                bucket_name = _bucket_of_proposal(conn, row["proposal_id"])
                rule = bucket_by_name(bucket_name or "")
                if rule is None:
                    raise FillError(
                        "この買いがどの枠のものか分かりません"
                        "（提案に紐づいていないか、知らない枠の名前です）"
                    )
                cash = select_cash(conn).get(fill["currency"], 0.0)
                result = apply_buy(existing, fill, rule, cash)
            else:
                result = apply_sell(existing, fill, opened_at, today)
        except FillError as exc:
            failed += 1
            mark_fill_failed(conn, row["id"], str(exc))
            print(f"反映できません #{row['id']} {fill['symbol']}: {exc}")
            continue

        save_fill_result(
            conn,
            row["id"],
            result.position,
            result.cash_delta,
            result.trade,
            fill["currency"],
        )
        if fill["side"] == "buy":
            _mark_proposal_taken(conn, row["proposal_id"])
        ok += 1
        print(
            f"反映しました #{row['id']} {fill['symbol']} {fill['side']} "
            f"{fill['quantity']}株 × {fill['price']:,.0f}円"
        )

    return ok, failed


def main() -> int:
    today = datetime.now(tz=JST).date()
    with connect() as conn:
        ok, failed = run(conn, today)
        capital = select_capital(conn)

    print(f"反映 {ok} 件 / 反映できず {failed} 件 / 総資金 {capital:,.0f}円")
    # 反映できなかったものがあっても、ジョブ自体は成功とする。
    # 理由は fills に残っており、画面に出るため。ここで失敗にすると、
    # 利用者の入力ミス1件でワークフロー全体が赤くなる。
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 8: テストが通ることを確認する**

Run: `uv run pytest tests/test_apply_fills.py -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 9: 全テストを通す**

Run: `uv run pytest -q`
Expected: 全て PASS

- [ ] **Step 10: コミット**

```bash
git add -A
git commit -m "feat: 申告された約定を保有・現金・履歴に反映するジョブ

未反映の fills を古い順に読み、trades / positions / cash に反映する。
1件ずつ1つのトランザクションで書くので、途中で止まっても帳尻は合う。

1件の失敗で全体を止めない。反映できなかった申告は消さず、理由を
fills.apply_error に残す。消すと利用者は「記録したはずなのに無い」
という状態に置かれ、原因も分からなくなる。

買いは提案に紐づいていないと、どの枠のものか決められないので拒否する。
反映が済んだ提案は outcome を taken にする（pending のまま残ると、
次の実行でも「まだ買っていない提案」として出てくるため）。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 6: 枠ごとの成績

**Files:**
- Modify: `src/investment/db.py`
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `db.select_bucket_performance(conn) -> list[dict]`
  各要素は `{"bucket": str, "closed": int, "wins": int, "win_rate": float | None,
  "total_pnl": float, "avg_holding_days": float | None, "breakeven_win_rate": float}`

- [ ] **Step 1: 失敗するテストを書く**

`tests/test_db.py` に追加する。

```python
def _sell_trade(conn, bucket: str, pnl: float, days: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency,
                 bucket, realized_pnl, holding_days)
            VALUES (NOW(), '1111.T', 'sell', 100, 1000, 'JPY', %s, %s, %s)
            """,
            (bucket, pnl, days),
        )
    conn.commit()


def test_bucket_performance_counts_only_closed_trades(conn):
    """成績は「売って決着した取引」だけで数える。買っただけの分は含めない。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO trades
                (executed_at, symbol, side, quantity, price, currency, bucket)
            VALUES (NOW(), '1111.T', 'buy', 100, 900, 'JPY', 'じっくり')
            """
        )
    conn.commit()
    _sell_trade(conn, "じっくり", pnl=20000, days=30)

    rows = {r["bucket"]: r for r in select_bucket_performance(conn)}
    assert rows["じっくり"]["closed"] == 1


def test_bucket_performance_reports_the_win_rate_and_the_total(conn):
    _sell_trade(conn, "じっくり", pnl=20000, days=30)
    _sell_trade(conn, "じっくり", pnl=-7000, days=12)
    _sell_trade(conn, "じっくり", pnl=15000, days=40)
    _sell_trade(conn, "回転", pnl=-3000, days=8)

    rows = {r["bucket"]: r for r in select_bucket_performance(conn)}

    patient = rows["じっくり"]
    assert patient["closed"] == 3
    assert patient["wins"] == 2
    assert patient["win_rate"] == pytest.approx(2 / 3)
    assert patient["total_pnl"] == pytest.approx(28000.0)
    assert patient["avg_holding_days"] == pytest.approx((30 + 12 + 40) / 3)

    fast = rows["回転"]
    assert fast["closed"] == 1
    assert fast["wins"] == 0
    assert fast["win_rate"] == pytest.approx(0.0)


def test_bucket_performance_includes_the_breakeven_win_rate_to_compare_against(conn):
    """勝率だけでは意味がない。損益トントンの勝率と並べて初めて判断できる。

    じっくり枠は 0.08 / (0.22 + 0.08) = 26.7%、
    回転枠は 0.05 / (0.10 + 0.05) = 33.3%。
    """
    rows = {r["bucket"]: r for r in select_bucket_performance(conn)}

    assert rows["じっくり"]["breakeven_win_rate"] == pytest.approx(0.2667, abs=0.001)
    assert rows["回転"]["breakeven_win_rate"] == pytest.approx(0.3333, abs=0.001)


def test_bucket_performance_lists_every_bucket_even_with_no_trades(conn):
    """まだ1件も決着していない枠も、0件として出す。

    行が消えると「まだ始まっていない」のか「集計から漏れた」のか分からない。
    """
    rows = select_bucket_performance(conn)

    assert [r["bucket"] for r in rows] == ["じっくり", "回転"]
    assert all(r["closed"] == 0 for r in rows)
    assert all(r["win_rate"] is None for r in rows)          # 0件なら勝率は「無い」
    assert all(r["avg_holding_days"] is None for r in rows)
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_db.py -q`
Expected: FAIL（`cannot import name 'select_bucket_performance'`）

- [ ] **Step 3: 実装する**

`src/investment/db.py` に追加する（冒頭の import に
`from investment.config import BUCKETS, ScreenCriteria` を加える。
`from investment.sizing import required_win_rate` も加える）。

```python
def select_bucket_performance(conn) -> list[dict]:
    """枠ごとの成績を返す。「どちらの型が向いているか」を測るための表。

    数えるのは売って決着した取引だけ。買っただけの分は勝ち負けが
    決まっていないので含めない。

    勝率だけでは判断できないので、損益トントンの勝率も一緒に返す。
    それを上回っていて初めて「効いている」と言える。

    まだ1件も決着していない枠も 0 件として返す。行が消えると
    「まだ始まっていない」のか「集計から漏れた」のか分からなくなる。
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT bucket,
                   COUNT(*)                                        AS closed,
                   COUNT(*) FILTER (WHERE realized_pnl > 0)        AS wins,
                   COALESCE(SUM(realized_pnl), 0)                  AS total_pnl,
                   AVG(holding_days)                               AS avg_holding_days
            FROM trades
            WHERE side = 'sell' AND bucket IS NOT NULL
            GROUP BY bucket
            """
        )
        by_name = {r["bucket"]: r for r in cur.fetchall()}

    result = []
    for b in BUCKETS:
        row = by_name.get(b.name)
        closed = int(row["closed"]) if row else 0
        wins = int(row["wins"]) if row else 0
        entry = 1000.0   # 率だけを求めるので、基準の値は何でもよい
        result.append({
            "bucket": b.name,
            "closed": closed,
            "wins": wins,
            "win_rate": (wins / closed) if closed else None,
            "total_pnl": float(row["total_pnl"]) if row else 0.0,
            "avg_holding_days": (
                float(row["avg_holding_days"])
                if row and row["avg_holding_days"] is not None
                else None
            ),
            "breakeven_win_rate": required_win_rate(
                entry,
                entry * (1 + b.take_profit_pct),
                entry * (1 - b.stop_loss_pct),
                0.0,
            ),
        })
    return result
```

**注意:** `db.py` が `investment.sizing` を import することで循環参照が起きないことを
確認すること（`sizing.py` は何も import していないので問題ない）。

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_db.py -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 5: コミット**

```bash
git add -A
git commit -m "feat: 枠ごとの成績を集計する

じっくり枠と回転枠のどちらが向いているかを測るための集計。
売って決着した取引だけを数える。

勝率と一緒に損益トントンの勝率（じっくり26.7% / 回転33.3%）を返す。
勝率だけでは判断できず、それを上回っていて初めて効いていると言える。

まだ1件も決着していない枠も0件として返す。行が消えると
「まだ始まっていない」のか「集計から漏れた」のか分からなくなる。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 7: 通知を送る

**Files:**
- Create: `src/investment/notify.py`
- Modify: `src/investment/db.py`
- Modify: `pyproject.toml`
- Test: `tests/test_notify.py`（新規）

**Interfaces:**
- Consumes: `db.record_gap`
- Produces: `db.select_push_subscriptions(conn) -> list[dict]`
- Produces: `db.delete_push_subscription(conn, endpoint) -> None`
- Produces: `notify.send(conn, title, body, url="/") -> tuple[int, int]`（送れた件数, 送れなかった件数）

**通知の方針（設計書 6.2）:** 「あなたが動く必要がある」ときだけ送る。
提案0件や、何も起きていない朝には送らない。

- [ ] **Step 1: 依存を足す**

`pyproject.toml` の `dependencies` に `"pywebpush>=2.0"` を加える。

Run: `uv sync`

- [ ] **Step 2: 失敗するテストを書く**

`tests/test_notify.py`（新規）:

```python
"""通知の送信のテスト。実際には送らず、送信の関数を差し替えて確認する。

差し替えるのは外部の通知サーバーへの送信だけで、宛先の読み込み・
失効した宛先の削除・失敗の記録は実際の処理を通す。
"""

from unittest.mock import patch

import pytest

from investment.notify import send


class FakeWebPushException(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.response = type("R", (), {"status_code": status_code})()


def _subs() -> list[dict]:
    return [
        {"endpoint": "https://push.test/a", "p256dh": "k1", "auth": "a1"},
        {"endpoint": "https://push.test/b", "p256dh": "k2", "auth": "a2"},
    ]


def test_send_delivers_to_every_registered_device():
    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()),
        patch("investment.notify.webpush") as push,
        patch("investment.notify.VAPID_PRIVATE_KEY", "dummy"),
        patch("investment.notify.VAPID_SUBJECT", "mailto:test@example.com"),
    ):
        ok, failed = send(conn=None, title="提案が3件", body="タップして確認")

    assert (ok, failed) == (2, 0)
    assert push.call_count == 2


def test_send_removes_a_device_that_no_longer_exists():
    """通知サーバーが404/410を返したら、その宛先は失効している。

    残したまま送り続けると毎回失敗が記録され、本当の失敗が埋もれる。
    """
    def fake_push(subscription_info, **kwargs):
        if subscription_info["endpoint"].endswith("/a"):
            raise FakeWebPushException(410)

    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()),
        patch("investment.notify.WebPushException", FakeWebPushException),
        patch("investment.notify.webpush", side_effect=fake_push),
        patch("investment.notify.delete_push_subscription") as delete,
        patch("investment.notify.record_gap"),
        patch("investment.notify.VAPID_PRIVATE_KEY", "dummy"),
        patch("investment.notify.VAPID_SUBJECT", "mailto:test@example.com"),
    ):
        ok, failed = send(conn=None, title="t", body="b")

    assert (ok, failed) == (1, 1)
    delete.assert_called_once()
    assert delete.call_args.args[1] == "https://push.test/a"


def test_send_records_a_failure_that_is_not_an_expired_device():
    """一時的な失敗は宛先を消さず、記録だけ残す。"""
    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()[:1]),
        patch("investment.notify.WebPushException", FakeWebPushException),
        patch("investment.notify.webpush", side_effect=FakeWebPushException(500)),
        patch("investment.notify.delete_push_subscription") as delete,
        patch("investment.notify.record_gap") as gap,
        patch("investment.notify.VAPID_PRIVATE_KEY", "dummy"),
        patch("investment.notify.VAPID_SUBJECT", "mailto:test@example.com"),
    ):
        ok, failed = send(conn=None, title="t", body="b")

    assert (ok, failed) == (0, 1)
    delete.assert_not_called()
    gap.assert_called_once()


def test_send_does_nothing_when_no_device_is_registered():
    with patch("investment.notify.select_push_subscriptions", return_value=[]):
        assert send(conn=None, title="t", body="b") == (0, 0)


def test_send_records_a_gap_when_the_key_is_missing_instead_of_crashing():
    """鍵が未設定でも、分析や朝の確認を巻き添えにしないこと。

    通知は補助であって本体ではない。届かなくてもアイコンを開けば同じ情報が見える。
    """
    with (
        patch("investment.notify.select_push_subscriptions", return_value=_subs()),
        patch("investment.notify.VAPID_PRIVATE_KEY", ""),
        patch("investment.notify.record_gap") as gap,
    ):
        ok, failed = send(conn=None, title="t", body="b")

    assert (ok, failed) == (0, 0)
    gap.assert_called_once()
    assert "鍵" in gap.call_args.kwargs["detail"]
```

- [ ] **Step 3: テストが失敗することを確認する**

Run: `uv run pytest tests/test_notify.py -q`
Expected: FAIL（`No module named 'investment.notify'`）

- [ ] **Step 4: データベース関数を実装する**

`src/investment/db.py` に追加する。

```python
def select_push_subscriptions(conn) -> list[dict]:
    """通知の宛先を返す。"""
    with conn.cursor() as cur:
        cur.execute("SELECT endpoint, p256dh, auth FROM push_subscriptions ORDER BY id")
        return [dict(r) for r in cur.fetchall()]


def delete_push_subscription(conn, endpoint: str) -> None:
    """失効した宛先を消す。

    通知サーバーが「その宛先はもう無い」と答えたときだけ呼ぶ。
    残したまま送り続けると毎回失敗が記録され、本当の失敗が埋もれる。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM push_subscriptions WHERE endpoint = %s", (endpoint,))
    conn.commit()
```

- [ ] **Step 5: `notify.py` を実装する**

`src/investment/notify.py`（新規）:

```python
"""スマホへの通知（Web Push）を送る。

**通知は補助であって本体ではない。** 届かなくても、ホーム画面のアイコンを
開けば同じ情報が見える。そのため、通知の失敗で分析や朝の確認を
巻き添えにしない。失敗した事実は data_gaps に残す。

送るのは「利用者が動く必要がある」ときだけ（設計書 6.2）。
提案0件や、何も起きていない朝には送らない。判断は呼び出し側が行う。
"""

import json
import os

from pywebpush import WebPushException, webpush

from investment.db import delete_push_subscription, record_gap, select_push_subscriptions

# VAPID は「この通知は確かに自分のサーバーが送った」と証明するための鍵。
# 秘密鍵は GitHub Secrets にだけ置く（Cloudflare 側には置かない）。
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "")

# 宛先が失効していることを表す応答。この2つのときだけ宛先を削除する。
EXPIRED_STATUS = (404, 410)


def send(conn, title: str, body: str, url: str = "/") -> tuple[int, int]:
    """登録されている全ての端末に通知を送る。戻り値は (送れた件数, 送れなかった件数)。

    url は通知をタップしたときに開く場所。
    """
    subscriptions = select_push_subscriptions(conn)
    if not subscriptions:
        return 0, 0

    if not VAPID_PRIVATE_KEY or not VAPID_SUBJECT:
        record_gap(
            conn,
            scope="notify:no_key",
            detail=(
                "通知の鍵（VAPID_PRIVATE_KEY / VAPID_SUBJECT）が設定されていないため、"
                f"{len(subscriptions)} 件の宛先に送れませんでした"
            ),
        )
        return 0, 0

    payload = json.dumps({"title": title, "body": body, "url": url})
    ok = failed = 0

    for sub in subscriptions:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except WebPushException as exc:
            failed += 1
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status in EXPIRED_STATUS:
                delete_push_subscription(conn, sub["endpoint"])
                continue
            record_gap(
                conn,
                scope="notify:failed",
                detail=f"通知を送れませんでした（宛先 {sub['endpoint']}）: {exc}",
            )
        else:
            ok += 1

    return ok, failed
```

- [ ] **Step 6: テストが通ることを確認する**

Run: `uv run pytest tests/test_notify.py -q && uv run ruff check .`
Expected: PASS

- [ ] **Step 7: 鍵を作る手順を書く**

`docs/setup-push.md`（新規）:

```markdown
# 通知（Web Push）の準備

## 1. 鍵を作る

「この通知は確かに自分の仕組みが送った」と証明するための鍵を作る。
1回だけ作れば、あとはずっと同じものを使う。

    export PATH="$HOME/.local/bin:$PATH"
    uv run python - <<'EOF'
    from py_vapid import Vapid01
    v = Vapid01()
    v.generate_keys()
    print("公開鍵 :", v.public_key_urlsafe_base64())
    print("秘密鍵 :", v.private_key_urlsafe_base64())
    EOF

**秘密鍵は誰にも見せない。** 公開鍵は画面に埋め込むので、見えて構わない。

## 2. GitHub に登録する

リポジトリの Settings → Secrets and variables → Actions → New repository secret

| 名前 | 値 |
|---|---|
| `VAPID_PRIVATE_KEY` | 上で出た秘密鍵 |
| `VAPID_SUBJECT` | `mailto:自分のメールアドレス` |

`VAPID_SUBJECT` は「送り主は誰か」を示すもの。通知サーバーが問題を見つけたときの
連絡先として使われる。メールアドレスの前に `mailto:` を付けること。

公開鍵は計画2-B（画面）で Cloudflare 側に登録する。それまで手元に控えておく。
```

- [ ] **Step 8: コミット**

```bash
git add -A
git commit -m "feat: スマホへの通知（Web Push）を送る

登録されている端末に通知を送る。通知サーバーが404/410を返した宛先は
失効しているので削除する（残すと毎回失敗が記録され、本当の失敗が埋もれる）。

通知は補助であって本体ではない。届かなくてもアイコンを開けば同じ情報が
見えるので、通知の失敗で分析や朝の確認を巻き添えにしない。鍵が未設定でも
例外を投げず、その事実を data_gaps に残すだけにする。

鍵の作り方と登録手順を docs/setup-push.md に書いた。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Task 8: 通知と反映をワークフローに繋ぐ

**Files:**
- Modify: `src/investment/jobs/apply_decision.py`
- Modify: `src/investment/jobs/morning_check.py`
- Modify: `.github/workflows/analyze.yml`
- Modify: `.github/workflows/morning.yml`
- Test: `tests/test_apply_decision.py`, `tests/test_morning_check.py`, `tests/test_migrate.py`

**Interfaces:**
- Consumes: `notify.send(conn, title, body, url)`, `apply_fills.run(conn, today)`

- [ ] **Step 1: 通知を出す条件のテストを書く**

`tests/test_apply_decision.py` に追加する。

```python
def test_main_notifies_when_there_are_proposals(tmp_path):
    """買うべき提案が出たときは通知する。"""
    # process が採用1件を返した状況を作る
    with (
        patch("investment.jobs.apply_decision.connect"),
        patch("investment.jobs.apply_decision.select_capital", return_value=550_000.0),
        patch("investment.jobs.apply_decision.load_decision",
              return_value=([{"symbol": "1111.T"}], "journal/x.md", None)),
        patch("investment.jobs.apply_decision.process", return_value=(1, 0)),
        patch("investment.jobs.apply_decision.json.loads", return_value={}),
        patch("investment.jobs.apply_decision.Path.read_text", return_value="{}"),
        patch("investment.jobs.apply_decision.notify_send") as notify,
    ):
        main()

    notify.assert_called_once()
    assert "1" in notify.call_args.kwargs["title"]


def test_main_does_not_notify_when_there_are_no_proposals(tmp_path):
    """提案が0件のときは通知しない。動く必要がないため。"""
    with (
        patch("investment.jobs.apply_decision.connect"),
        patch("investment.jobs.apply_decision.select_capital", return_value=550_000.0),
        patch("investment.jobs.apply_decision.load_decision",
              return_value=([], "journal/x.md", None)),
        patch("investment.jobs.apply_decision.process", return_value=(0, 0)),
        patch("investment.jobs.apply_decision.json.loads", return_value={}),
        patch("investment.jobs.apply_decision.Path.read_text", return_value="{}"),
        patch("investment.jobs.apply_decision.notify_send") as notify,
    ):
        main()

    notify.assert_not_called()
```

`tests/test_morning_check.py` に追加する。

```python
def test_main_notifies_when_something_may_have_been_executed():
    """損切り・利確に到達した可能性があるときは通知する。"""
    hits = [{"symbol": "1111.T", "kind": "stop_loss", "estimated_price": 828,
             "day_high": 900, "day_low": 820}]
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "じっくり",
                             "opened_at": date(2026, 9, 1)}]),
        patch("investment.jobs.morning_check.run", return_value=(hits, 0, 1)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_called_once()


def test_main_does_not_notify_on_a_quiet_morning():
    """何も起きていない朝は通知しない。"""
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "じっくり",
                             "opened_at": date(2026, 9, 1)}]),
        patch("investment.jobs.morning_check.run", return_value=([], 0, 1)),
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        main()

    notify.assert_not_called()


def test_main_notifies_when_a_position_passed_its_deadline():
    """回転枠の期限切れは、値動きが無くても知らせる。降りる必要があるため。"""
    old = date(2026, 8, 24)
    with (
        patch("investment.jobs.morning_check.connect"),
        patch("investment.jobs.morning_check.select_positions",
              return_value=[{"symbol": "1111.T", "bucket": "回転", "opened_at": old}]),
        patch("investment.jobs.morning_check.run", return_value=([], 0, 1)),
        patch("investment.jobs.morning_check.datetime") as dt,
        patch("investment.jobs.morning_check.notify_send") as notify,
    ):
        dt.now.return_value.date.return_value = date(2026, 9, 7)
        main()

    notify.assert_called_once()
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_apply_decision.py tests/test_morning_check.py -q`
Expected: FAIL（`notify_send` が無い）

- [ ] **Step 3: `apply_decision.main()` に通知を足す**

`src/investment/jobs/apply_decision.py`:

```python
from investment.notify import send as notify_send
```

`main()` の `process(...)` の戻り値を受け取り、採用が1件以上のときだけ通知する。

```python
        capital = select_capital(conn)
        print(f"総資金 {capital:,.0f}円 で検証します")
        accepted, _rejected = process(conn, ctx, decisions, journal_path, SETTINGS, capital)

        # 「あなたが動く必要がある」ときだけ通知する。
        # 提案が0件の日に通知すると、通知そのものが意味を失う。
        if accepted:
            notify_send(
                conn,
                title=f"買う候補が {accepted} 件あります",
                body="タップして内容を確認してください",
                url="/",
            )
```

- [ ] **Step 4: `morning_check.main()` に通知を足す**

`src/investment/jobs/morning_check.py`:

```python
from investment.notify import send as notify_send
```

```python
    expired = find_expired([dict(p) for p in positions], today)
    lines, code = summarize_result(len(positions), hits, failed, attempted, expired)
    for line in lines:
        print(line)

    # 到達の可能性か期限切れがあるときだけ通知する。
    # 何も起きていない朝に通知すると、通知そのものが意味を失う。
    if hits or expired:
        parts = []
        if hits:
            parts.append(f"{len(hits)} 件が約定した可能性")
        if expired:
            parts.append(f"{len(expired)} 件が期限切れ")
        with connect() as conn:
            notify_send(
                conn,
                title="確認してください",
                body="、".join(parts),
                url="/holdings",
            )
    return code
```

**注意:** `main()` の既存の `with connect() as conn:` ブロックは
`run()` の呼び出しで閉じている。通知のために接続を開き直すのは、
株価の取得中に接続を保持しないという既存の方針（`run_screen` / `build_context` と同じ）に沿う。

- [ ] **Step 5: テストが通ることを確認する**

Run: `uv run pytest -q && uv run ruff check .`
Expected: 全て PASS

- [ ] **Step 6: ワークフローに反映ジョブと鍵を足す**

`.github/workflows/analyze.yml` の「データベースの構造を最新にする」の直後に追加する。

```yaml
      - name: 記録された約定を反映する
        # スマホから「買った/売った」と記録された内容を、保有・現金・履歴に
        # 反映する。分析より先に行う。反映しないまま分析すると、
        # 既に買った銘柄をもう一度勧めることになる。
        run: uv run python -m investment.jobs.apply_fills
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          PYTHONPATH: src
```

`.github/workflows/analyze.yml` の「判断を検証して保存する」の `env:` に鍵を足す。

```yaml
        env:
          DATABASE_URL: ${{ secrets.DATABASE_URL }}
          VAPID_PRIVATE_KEY: ${{ secrets.VAPID_PRIVATE_KEY }}
          VAPID_SUBJECT: ${{ secrets.VAPID_SUBJECT }}
          PYTHONPATH: src
```

`.github/workflows/morning.yml` にも同じ「記録された約定を反映する」ステップを
「データベースの構造を最新にする」の直後に足し、`morning_check` の `env:` に
同じ2つの鍵を足す。

- [ ] **Step 7: ワークフローの順序を守るテストを足す**

`tests/test_migrate.py` に追加する。

```python
@pytest.mark.parametrize("workflow", ["analyze.yml", "morning.yml"])
def test_fills_are_applied_before_anything_reads_the_positions(workflow: str):
    """記録の反映を、保有を読む処理より先に行うこと。

    反映しないまま分析すると、既に買った銘柄をもう一度勧めることになる。
    朝の確認も、買ったばかりの銘柄を見落とす。
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "investment.jobs.apply_fills" in text, f"{workflow} に反映の手順がありません"

    readers = ("investment.jobs.build_context", "investment.jobs.morning_check")
    first_reader = min(text.index(m) for m in readers if m in text)
    assert text.index("investment.jobs.apply_fills") < first_reader, (
        f"{workflow} で、記録の反映が保有を読む処理より後になっています"
    )


@pytest.mark.parametrize("workflow", ["analyze.yml", "morning.yml"])
def test_the_notification_keys_reach_the_step_that_sends_them(workflow: str):
    """通知を送るステップに鍵が渡っていること。

    渡し忘れると、鍵が無いという記録だけが毎回積もる。
    """
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    assert "VAPID_PRIVATE_KEY" in text
    assert "VAPID_SUBJECT" in text
```

- [ ] **Step 8: 全テストと静的検査を通す**

Run: `uv run ruff check . && uv run pytest -q`
Expected: 全て PASS

- [ ] **Step 9: ワークフローが YAML として正しいことを確認する**

```bash
uv run --with pyyaml python - <<'EOF'
import yaml
for f in ("analyze.yml", "morning.yml", "screen.yml"):
    d = yaml.safe_load(open(f".github/workflows/{f}"))
    job = next(iter(d["jobs"].values()))
    print(f"--- {f} ---")
    for i, s in enumerate(job["steps"], 1):
        label = s.get("name") or s.get("uses") or ("run: " + str(s.get("run","")).strip().splitlines()[0][:45])
        print(f"  {i}. {label}")
EOF
```

Expected: どちらも「データベースの構造を最新にする」→「記録された約定を反映する」の順になっている

- [ ] **Step 10: 通しで動くことを確認する**

使い捨てのデータベースで、記録から反映までを一度通す。

```bash
export DATABASE_URL_TEST="postgresql://postgres:test@localhost:55432/investtest"
uv run python - <<'EOF'
import os
from datetime import date
from investment.db import (apply_migrations, connect, init_cash,
                           select_bucket_performance, select_capital)
from investment.jobs.apply_fills import run

with connect(os.environ["DATABASE_URL_TEST"]) as conn:
    apply_migrations(conn)
    with conn.cursor() as cur:
        cur.execute("TRUNCATE trades, positions, proposals, cash, data_gaps, fills")
    conn.commit()
    init_cash(conn, jpy=550_000)
    print("開始時の総資金:", select_capital(conn))

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO proposals (created_at, symbol, action, quantity, entry_price,
                take_profit, stop_loss, required_win_rate, rationale, scenario,
                confidence, strategy_tag, bucket, rule_version, journal_path)
            VALUES (NOW(), '1111.T', 'buy', 100, 900, 1098, 828, 0.2667,
                    'x', 'y', 'mid', 'z', 'じっくり', 'v3', 'j.md') RETURNING id
        """)
        pid = cur.fetchone()["id"]
        cur.execute("""
            INSERT INTO fills (proposal_id, symbol, side, quantity, price, currency)
            VALUES (%s, '1111.T', 'buy', 100, 900, 'JPY')
        """, (pid,))
    conn.commit()

    print("買いの反映:", run(conn, today=date(2026, 9, 8)))
    print("買った後の総資金:", select_capital(conn))

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO fills (symbol, side, quantity, price, currency)
            VALUES ('1111.T', 'sell', 100, 1100, 'JPY')
        """)
    conn.commit()

    print("売りの反映:", run(conn, today=date(2026, 9, 30)))
    print("売った後の総資金:", select_capital(conn))
    for r in select_bucket_performance(conn):
        print(f"  {r['bucket']}: {r['closed']}件 勝率={r['win_rate']} 損益={r['total_pnl']}")
EOF
```

Expected:
```
開始時の総資金: 550000.0
買いの反映: (1, 0)
買った後の総資金: 550000.0      ← 現金が減り、保有が増えるので合計は変わらない
売りの反映: (1, 0)
売った後の総資金: 570000.0      ← 20,000円の利益が資金に加わった
  じっくり: 1件 勝率=1.0 損益=20000.0
  回転: 0件 勝率=None 損益=0.0
```

**この確認がこの計画の合否そのものである。** 「利確して資金が増え、次に買える額が増える」
という循環が数字で成立していることを、ここで見る。

- [ ] **Step 11: コミット**

```bash
git add -A
git commit -m "feat: 記録の反映と通知をワークフローに繋ぐ

analyze と morning の両方で、保有を読む処理より先に記録の反映を行う。
反映しないまま分析すると、既に買った銘柄をもう一度勧めることになる。
順序が守られていることをテストで固定した。

通知は「動く必要があるとき」だけ送る。
- 買うべき提案が出た（0件のときは送らない）
- 損切り・利確に到達した可能性がある（何も無い朝は送らない）
- 回転枠の期限が切れた

通しの確認: 550,000円 → 100株を900円で買う（総資金は変わらない）
→ 1,100円で売る → 総資金 570,000円。枠ごとの成績に1件計上される。
「利確して資金が増え、次に買える額が増える」という循環が成立している。

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## 完成の条件

- [ ] `select_capital` が現金と保有から総資金を計算し、現状で 550,000.0 を返す
- [ ] 資金が増えると、1銘柄に投じられる額と買える株数が増える
- [ ] `fills` に1行入れて `apply_fills` を走らせると、保有・現金・履歴が正しく動く
- [ ] 反映できない申告は消えず、理由が残る。1件の失敗が他を止めない
- [ ] 同じ申告を二度反映しない
- [ ] 買いを反映すると、その提案が `taken` になる
- [ ] 枠ごとの成績（決着数・勝率・損益・平均保有日数・損益トントンの勝率）が出る
- [ ] 提案が出たときだけ通知が送られる。0件なら送らない
- [ ] 到達の可能性か期限切れがあるときだけ、朝の通知が送られる
- [ ] 通知の鍵が未設定でも、分析や朝の確認は成功する
- [ ] `analyze` と `morning` で、記録の反映が保有を読む処理より先にある
- [ ] `uv run ruff check .` と `uv run pytest -q` が両方通る

## 計画2-B に持ち越すもの

- Cloudflare Pages のプロジェクト作成と Access の設定
- Pages Functions（`/api/state`、`/api/fills`、`/api/analyze`、`/api/subscribe`、
  `/api/proposals/{id}/skip`）
- 画面3つ（今日やること / 保有 / 収支）
- 通知の購読登録と、ホーム画面への追加を促す案内
- Service Worker（通知を受け取って表示する部分）
