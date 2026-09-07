-- 「見送る」ときに利用者が入力した理由を残すための列。
--
-- 見送るときになぜそう判断したかは、この仕組みが存在する理由そのもの
-- （あとで振り返って学ぶための材料）なので、黙って捨てずに保存する。
-- 理由の入力は任意なので NULL を許す。

ALTER TABLE proposals ADD COLUMN IF NOT EXISTS skip_reason TEXT;
