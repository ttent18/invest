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
-- DO ブロックにしているのは、ALTER TABLE ... ADD CONSTRAINT に
-- IF NOT EXISTS が無く、2回目の実行で失敗してしまうため
-- （マイグレーションは何度実行しても安全である必要がある）。
DO $$
BEGIN
    ALTER TABLE proposals ALTER COLUMN bucket SET NOT NULL;
    ALTER TABLE positions ALTER COLUMN bucket SET NOT NULL;
EXCEPTION WHEN OTHERS THEN NULL;
END $$;

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
