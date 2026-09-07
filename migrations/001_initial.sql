-- 銘柄のファンダメンタルズ（週1回のバッチで更新する）
CREATE TABLE IF NOT EXISTS fundamentals (
    symbol            TEXT        NOT NULL,
    as_of             DATE        NOT NULL,
    name              TEXT        NOT NULL,
    market_cap        DOUBLE PRECISION,
    revenue_growth    DOUBLE PRECISION,
    operating_margin  DOUBLE PRECISION,
    roe               DOUBLE PRECISION,
    equity_ratio      DOUBLE PRECISION,
    PRIMARY KEY (symbol, as_of)
);

CREATE INDEX IF NOT EXISTS idx_fundamentals_as_of ON fundamentals (as_of DESC);

-- 現金残高（通貨ごと）
CREATE TABLE IF NOT EXISTS cash (
    currency  TEXT             PRIMARY KEY,
    amount    NUMERIC(18, 4)   NOT NULL
);

-- 約定履歴。追記のみ。UPDATE も DELETE も行わない
CREATE TABLE IF NOT EXISTS trades (
    id           BIGSERIAL      PRIMARY KEY,
    executed_at  TIMESTAMPTZ    NOT NULL,
    symbol       TEXT           NOT NULL,
    side         TEXT           NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity     INTEGER        NOT NULL CHECK (quantity > 0),
    price        NUMERIC(18, 4) NOT NULL CHECK (price > 0),
    currency     TEXT           NOT NULL,
    fee          NUMERIC(18, 4) NOT NULL DEFAULT 0,
    is_virtual   BOOLEAN        NOT NULL DEFAULT TRUE
);

-- 現在の保有
CREATE TABLE IF NOT EXISTS positions (
    symbol        TEXT           PRIMARY KEY,
    quantity      INTEGER        NOT NULL CHECK (quantity > 0),
    avg_price     NUMERIC(18, 4) NOT NULL CHECK (avg_price > 0),
    currency      TEXT           NOT NULL,
    take_profit   NUMERIC(18, 4) NOT NULL,
    stop_loss     NUMERIC(18, 4) NOT NULL,
    opened_at     TIMESTAMPTZ    NOT NULL
);

-- AIの提案。採否にかかわらず全件残す
CREATE TABLE IF NOT EXISTS proposals (
    id                BIGSERIAL      PRIMARY KEY,
    created_at        TIMESTAMPTZ    NOT NULL,
    symbol            TEXT           NOT NULL,
    action            TEXT           NOT NULL CHECK (action IN ('buy', 'sell')),
    quantity          INTEGER        NOT NULL CHECK (quantity > 0),
    entry_price       NUMERIC(18, 4) NOT NULL,
    take_profit       NUMERIC(18, 4) NOT NULL,
    stop_loss         NUMERIC(18, 4) NOT NULL,
    required_win_rate DOUBLE PRECISION NOT NULL,
    rationale         TEXT           NOT NULL,
    scenario          TEXT           NOT NULL,
    confidence        TEXT           NOT NULL CHECK (confidence IN ('low', 'mid', 'high')),
    strategy_tag      TEXT           NOT NULL,
    rule_version      TEXT           NOT NULL,
    journal_path      TEXT           NOT NULL,
    outcome           TEXT           NOT NULL DEFAULT 'pending'
                                     CHECK (outcome IN ('pending', 'taken', 'skipped'))
);

-- 取得に失敗した日の記録。判断をスキップした事実を残す
CREATE TABLE IF NOT EXISTS data_gaps (
    id          BIGSERIAL   PRIMARY KEY,
    occurred_at TIMESTAMPTZ NOT NULL,
    scope       TEXT        NOT NULL,
    detail      TEXT        NOT NULL
);
