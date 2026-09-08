-- 画面に含み損益を出すために、最後に見た株価を保有に持たせる。
--
-- Cloudflare 側からは株価を取れない（取得元のライブラリがあの環境で動かない）。
-- 朝の確認が既に各保有の値動きを取りに行っているので、そのとき見た値を残す。
--
-- last_price_at を一緒に持つのは、「いつ時点の値か」を画面に出すため。
-- 15〜20分遅れの値をリアルタイムに見せかけないという方針による。

ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_price    NUMERIC(18, 4);
ALTER TABLE positions ADD COLUMN IF NOT EXISTS last_price_at TIMESTAMPTZ;
