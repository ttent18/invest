# 銘柄リスト拡充・候補0件記録・rules/v1.md 日付修正 — 報告書

## 概要

計画1マージ後の3つの修正を実装した。

1. `data/symbols_jp.txt` を10銘柄（大型株中心）から、JPX公式データに基づく
   内国株式3市場（プライム・スタンダード・グロース）全3,707銘柄に置き換え
2. `apply_decision.py`: 提案が0件（採用も却下も0件）だった日を `data_gaps` に記録するよう修正
3. `rules/v1.md` の採用日を 2026-09-08 → 2026-09-07 に修正（未来日だった）

---

## 修正1: 銘柄リストを全上場銘柄にする

### 作ったもの

- `scripts/update_symbols.py`（新規）
  - JPX の `https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx`
    をダウンロードし、`市場・商品区分` が「プライム（内国株式）」「スタンダード（内国株式）」
    「グロース（内国株式）」の3つに該当する行の銘柄コードだけを抜き出して
    `data/symbols_jp.txt` に書き出す
  - 除外した区分とその理由をコードコメントと生成ファイルのヘッダーに明記:
    - ETF・ETN / REIT・ベンチャーファンド・カントリーファンド・インフラファンド:
      個別企業ではないため財務指標によるスクリーニングが意味を持たない
    - PRO Market: 特定投資家向け市場で個人は売買できない
    - 外国株式（プライム/スタンダード/グロース）・出資証券: 対象外
  - ダウンロード処理 (`download()`) とフィルタ処理 (`extract_domestic_codes()`) を分離し、
    フィルタ処理だけをネットワーク無しでテスト可能にした
  - 出力ファイルの先頭に `#` で始まるコメント行として、生成日時・出所URL・
    元データの日付・件数・除外理由・更新方法を記録（`load_symbols` が `#` 行を
    無視する実装であることを `src/investment/jobs/run_screen.py:30-33` で確認済み）

- `pyproject.toml`
  - `dependencies` に `openpyxl>=3.1.5` を追加（`uv add openpyxl` で追加、`uv.lock` も更新済み）
  - `pandas` は既に `yfinance` の依存として入っていたため追加不要（実際に
    `import pandas` はエラー無く成功することを確認済み）
  - `[tool.pytest.ini_options]` の `pythonpath` に `"scripts"` を追加し、
    `tests/test_update_symbols.py` から `import update_symbols` できるようにした

- `tests/test_update_symbols.py`（新規、8件）
  - `extract_domestic_codes()` が3つの内国株式区分だけを含み、ETF・ETN /
    REIT / PRO Market / 外国株式 / 出資証券を除外することを、実データを
    模した小さな DataFrame で検証
  - コード列が int（数字だけのコード）と str（英字混じりコード、例: 130A）の
    両方を正しく文字列化することを検証
  - `build_symbols_file()` が出力するヘッダーは全行 `#` で始まり、
    `load_symbols()`（既存コード）に通しても銘柄コードだけが残ることを確認
  - ヘッダーに出所URL・元データの日付・件数・生成日時が含まれることを確認
  - ダウンロード処理 (`download()`) 自体はテスト対象外（要求どおり）

### 実行方法

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/update_symbols.py
```

JPXのファイルは月次更新なので、月次を目安に再実行して差分をコミットする。
これは `data/symbols_jp.txt` の先頭コメントと `scripts/update_symbols.py`
のモジュール docstring の両方に明記した。

### 生成結果の検証

実際にスクリプトを実行し、以下を確認した（本番DBには一切接続していない。
ネットワークはJPXの公開xlsxのダウンロードのみ使用）。

- **総件数: 3,707件**（プライム1,556 + スタンダード1,555 + グロース596 =
  3,707。タスク記載の想定通り）
- 元データの日付: `20260831`（xlsx内の `日付` 列の値。JPXの月次更新データ）
- 重複: 0件
- **先頭5件:** `1301, 130A, 1332, 1333, 135A`
- **末尾5件:** `9991, 9993, 9994, 9996, 9997`
- **ETF/REITの混入確認:** `1305`（ダイヤモンド信託 ETF）・`1306`（TOPIX連動型
  上場投資信託）は元データで `市場・商品区分 = "ETF・ETN"` であることを
  確認済みで、生成後の `data/symbols_jp.txt` に **含まれていない**ことを
  `grep -xE '1305|1306' data/symbols_jp.txt` で確認（該当なし）
- `load_symbols()`（既存の読み込み関数）に通した結果も件数3,707・重複0件で一致

生成された `data/symbols_jp.txt` の先頭:

```
# 生成日時: 2026-09-07T19:07:04.887575+09:00
# 出所: https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xlsx
# 元データの日付: 20260831
# 件数: 3707
#
# 対象: 内国株式のプライム・スタンダード・グロース市場のみ
#   (ETF・ETN、REIT等、PRO Market、外国株式、出資証券は除外)
#
# 更新方法: uv run python scripts/update_symbols.py
# (JPXのファイルは月次更新。更新のたびに再実行してコミットすること)
1301
130A
1332
...
```

---

## 修正2: 候補0件の日が記録されない

`src/investment/jobs/apply_decision.py` を変更:

- `record_no_proposals(conn, ctx, decisions)` を新設。`data_gaps` に
  `scope="analysis:no_proposals"` で記録する。`detail` には
  - `ctx["candidates"]` の件数
  - `ctx["positions"]` の件数
  - AIが出した `decisions` の件数（このケースでは常に0）
  を全て含め、標準出力にも同じ文言を出す。
  例: `候補 0 件 / 保有 0 件 に対し、AIが出した decisions は 0 件でした（採用・却下とも0件）。`
  候補件数が detail に含まれるため、「候補が0件だった」（`候補 0 件`）のか
  「候補はあったがAIが全部見送った」（`候補 N 件`, N>0）のかを区別できる。

- `main()` の中身を `process(conn, ctx, decisions, journal_path, settings)`
  に切り出した。これまでの `if accepted or rejected:` によるスキップを廃止し、
  常に DB 接続して以下のいずれかを行うようにした:
  - `accepted` があれば `insert_proposals`
  - `rejected` があれば `record_rejections`
  - **両方とも0件なら `record_no_proposals`（今回追加）**

  これにより「毎回DBに何かが記録される」状態になり、記録の欠落が起きなくなった。

### テスト（`tests/test_apply_decision.py` に追加、5件）

- `test_process_records_gap_when_no_candidates_and_no_decisions`:
  候補0件・decisions0件のとき `record_gap` が呼ばれ、
  `scope == "analysis:no_proposals"`、`detail` に「候補 0」が含まれることを確認
- `test_process_records_gap_when_candidates_present_but_zero_decisions`:
  候補1件・decisions0件（AIが何も出さなかった）のときも記録され、
  `detail` に「候補 1」が含まれることを確認
- `test_no_candidates_and_candidates_present_cases_are_distinguishable`:
  上記2ケースの `detail` が異なる文字列になることを確認（区別できることの直接テスト）
- `test_process_does_not_record_gap_when_something_accepted`:
  1件でも採用されるとき `record_gap` が呼ばれない（`insert_proposals` は呼ばれる）ことを確認
- `test_process_does_not_record_gap_when_something_rejected`:
  1件でも却下があるとき `record_gap` が呼ばれない（`record_rejections` は呼ばれる）ことを確認

---

## 修正3: rules/v1.md の採用日修正

`rules/v1.md` の「採用日: 2026-09-08」を「採用日: 2026-09-07」に修正。
他に日付 `2026-09-08` を参照している箇所がないか `grep` で確認したところ、
`.claude/commands/analyze.md` 内の `decision.json` の**サンプル**
（`"journal_path": "journal/2026-09-08.md"`）だけがヒットしたが、これは
将来の日付の例示にすぎずロジックとは無関係、かつタスク指示で
`.claude/commands/analyze.md` は「触らないこと」に明記されているため変更していない。

---

## RED / GREEN の証拠

### RED（実装前）

```
$ uv run pytest tests/test_update_symbols.py -v
ImportError while importing test module '.../tests/test_update_symbols.py'.
E   ModuleNotFoundError: No module named 'update_symbols'

$ uv run pytest tests/test_apply_decision.py -v -k "process or no_candidates_and_candidates"
5 failed:
E   ImportError: cannot import name 'process' from 'investment.jobs.apply_decision'
```

### GREEN（実装後・全件）

```
$ uv run pytest -v
======================== 124 passed, 9 skipped in 0.81s ========================
```

9件のスキップは全て `@pytest.mark.integration`（`DATABASE_URL_TEST` が
未設定の場合にスキップする設計）。Docker のテスト用Postgresを起動して
`DATABASE_URL_TEST=postgresql://postgres:testpass@localhost:55432/investtest`
を設定して再実行したところ、**133件全て（integrationを含む）が成功**した:

```
$ docker run -d --name invest-test-pg -e POSTGRES_PASSWORD=testpass \
    -e POSTGRES_DB=investtest -p 55432:5432 postgres:18-alpine
$ export DATABASE_URL_TEST="postgresql://postgres:testpass@localhost:55432/investtest"
$ export PYTHONPATH=src
$ uv run pytest -v
======================= 133 passed in 1.31s =======================
```

テスト後、コンテナは `docker stop && docker rm` で削除済み（指示どおり
検証用コンテナは残していない）。**本番Neonには一度も接続していない。**

内訳: 元120件 → 新規追加13件（修正1: 8件、修正2: 5件） = 133件。

## ruff check の終了コード

```
$ uv run ruff check .
All checks passed!
$ echo $?
0
```

---

## スクリプトの実行方法（再掲）

```bash
export PATH="$HOME/.local/bin:$PATH"
uv run python scripts/update_symbols.py
```

---

## 懸念

1. **`data/symbols_jp.txt` の差分が非常に大きい**（10行→3,717行、うち
   ヘッダー10行＋銘柄3,707件）。レビュー時に diff が読みにくい可能性がある。
   内容は機械生成でありレビューは「件数と先頭/末尾の妥当性確認」で足りると考えるが、
   念のため報告する。

2. **週次バッチの所要時間。** `run_screen.py` は1銘柄あたり約0.75秒
   （+ `time.sleep(0.1)`）。3,707銘柄では概算で
   `3707 × (0.75 + 0.1) ≒ 3,150秒 ≒ 52.5分` となり、タスクで示唆された
   「50分程度に収まる見込み」とほぼ一致する。`timeout-minutes: 120`
   （`.github/workflows/screen.yml`）は変更していないため、想定通りなら
   問題ないはずだが、実運用でGitHub Actions上の実測値（ローカルより遅い
   可能性がある）を一度確認することを推奨する。

3. **`process()` へのリファクタリングで `main()` の挙動が変わった箇所。**
   従来は `accepted` も `rejected` も空のとき DB 接続すら行わなかったが、
   修正後は常に接続する（`record_no_proposals` のため）。これは修正2の
   目的そのものなので意図した変更だが、「DB接続が増える」という副作用が
   ある点は明記しておく（1回のジョブ実行につき最大1回の追加接続で、
   接続失敗時は他ジョブ同様に例外で落ちる。既存の `insert_proposals` /
   `record_rejections` と同じ接続を使い回すため接続数自体は増えていない）。

4. **JPXのxlsxは英字を含む新形式コード（例: `130A`）を含む。**
   `market.py` の `normalize_symbol` は既にこの形式に対応済み
   （`len(s) == 4 and s[0].isdigit() and s.isalnum()`）であることを
   確認済みなので、今回の変更に伴う追加対応は不要と判断した。
