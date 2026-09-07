-- ルール v3: 4つの枠を「じっくり」「回転」の2種類に分けた。
-- どちらの枠で買ったかを記録し、あとから枠ごとの成績を比べられるようにする。
-- これが無いと、2つの型のどちらが効いているかを測るという v3 の目的が果たせない。
--
-- 既存の行（v1/v2 の提案2件）は +22%/-8% で出したものなので「じっくり」に当たる。
-- 実態に合っているため、そう埋める（不明として NULL にはしない）。

ALTER TABLE proposals ADD COLUMN IF NOT EXISTS bucket TEXT;
UPDATE proposals SET bucket = 'じっくり' WHERE bucket IS NULL;

ALTER TABLE positions ADD COLUMN IF NOT EXISTS bucket TEXT;
UPDATE positions SET bucket = 'じっくり' WHERE bucket IS NULL;

-- 制約は値を埋めたあとで付ける。
--
-- SET NOT NULL は何度実行しても安全（2回目は何もしない）ので、そのまま書く。
-- 以前はここも例外を握りつぶす DO ブロックに入れていたが、それをやると
-- ロック待ちや権限の問題で NOT NULL が付かなかったときに、何のエラーも
-- 出ないまま列が NULL 許容のまま残る。しかも下の CHECK 制約は NULL を
-- 弾かない（NULL との比較は偽ではなく NULL になるため）ので、
-- 枠の記録が無い行が黙って溜まり、枠ごとの成績比較という v3 の目的が
-- 果たせなくなる。失敗したら失敗として見えるようにする。
ALTER TABLE proposals ALTER COLUMN bucket SET NOT NULL;
ALTER TABLE positions ALTER COLUMN bucket SET NOT NULL;

-- 一方 ADD CONSTRAINT には IF NOT EXISTS が無く、2回目の実行で必ず失敗する。
-- こちらは「既にある」場合だけを捕まえる（duplicate_object のみ。
-- それ以外のエラーはそのまま外へ出す）。
DO $$
BEGIN
    ALTER TABLE proposals ADD CONSTRAINT proposals_bucket_check
        CHECK (bucket IN ('じっくり', '回転'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$
BEGIN
    ALTER TABLE positions ADD CONSTRAINT positions_bucket_check
        CHECK (bucket IN ('じっくり', '回転'));
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;
