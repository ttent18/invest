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
