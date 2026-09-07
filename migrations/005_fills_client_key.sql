-- 画面から記録するときに必要な2つの列。
--
-- client_key: 画面のボタンを2回押しても記録が2行にならないようにするための鍵。
--   画面側が送信ごとに一意の値を作り、同じ鍵なら2行目を作らない。
--   計画2-A で入れた「同じ提案に2回申告が来たら弾く」守りは、2行目を
--   別の申告として扱うのですり抜けるため、行そのものを作らせない。
--   手作業で入れる場合は NULL でよい（UNIQUE は NULL を重複とみなさない）。
--
-- traded_at: 利用者が実際に売買した日時。画面で入力する。
--   recorded_at（入力した時刻）を使うと、翌日に入力したときに1日ずれる。
--   保有日数の正確さは「どちらの枠が向いているか」の測定に直結する。

ALTER TABLE fills ADD COLUMN IF NOT EXISTS client_key TEXT;
ALTER TABLE fills ADD COLUMN IF NOT EXISTS traded_at  TIMESTAMPTZ;

-- UNIQUE 制約は裏で索引を作る。2回目の実行で衝突したときに出るエラーは
-- 002 の CHECK 制約（duplicate_object）とは種類が違い、索引の名前が
-- 重複した扱い（duplicate_table）で返ってくる。実際に3回続けて流して
-- 確かめたところ、duplicate_object だけを捕まえる書き方（002 のお手本の
-- ままの書き方）では2回目の実行が失敗した。ここでは両方を捕まえる
-- （WHEN OTHERS は使わない。それ以外のエラーはそのまま外へ出す）。
DO $$
BEGIN
    ALTER TABLE fills ADD CONSTRAINT fills_client_key_unique UNIQUE (client_key);
EXCEPTION WHEN duplicate_object OR duplicate_table THEN NULL;
END $$;
