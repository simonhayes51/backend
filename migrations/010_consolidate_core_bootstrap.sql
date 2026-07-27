-- Migration 010 (consolidate_core_bootstrap): consolidate main.py's inline
-- core-DB bootstrap
-- target: core
--
-- Every statement below used to run inline in main.py's lifespan() on
-- every boot, predating this migration runner and duplicating/shadowing
-- its authority (schema authority for the core DB was split between this
-- runner and that inline block - see the audit that led to this file).
-- This migration is the exact same DDL, moved here verbatim, so applying
-- it against a database where these tables already exist is a true no-op
-- (every statement is IF NOT EXISTS / ADD COLUMN IF NOT EXISTS). The
-- inline block itself is deleted from main.py's lifespan() in the same
-- change that adds this file.
--
-- Named "010_consolidate_core_bootstrap" (not 015) so it sorts and runs
-- BEFORE 010_core_overhaul.sql, which does `ALTER TABLE api_keys ...` and
-- creates api_key_usage with a FK to api_keys - both assuming api_keys
-- already exists. This table creates api_keys, so it must run first on a
-- genuinely fresh database (two files sharing a numeric prefix already has
-- precedent here: 006_multiple_images.sql / 006_trader_payment_accounts.sql).

CREATE TABLE IF NOT EXISTS trades (
    user_id TEXT NOT NULL,
    player TEXT NOT NULL,
    version TEXT NOT NULL,
    buy INTEGER NOT NULL,
    sell INTEGER NOT NULL,
    quantity INTEGER NOT NULL DEFAULT 1,
    platform TEXT NOT NULL,
    profit INTEGER NOT NULL DEFAULT 0,
    ea_tax INTEGER NOT NULL DEFAULT 0,
    tag TEXT,
    notes TEXT,
    timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trade_id BIGINT
);
CREATE UNIQUE INDEX IF NOT EXISTS trades_user_trade_uidx ON trades (user_id, trade_id);
DROP INDEX IF EXISTS idx_trades_date;
CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(user_id, tag);
CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(user_id, platform);

-- users (plan/premium/roles read by compute_entitlements)
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY,
  plan TEXT,
  premium_until TIMESTAMPTZ,
  roles JSONB DEFAULT '[]',
  password_hash TEXT
);
ALTER TABLE users ADD COLUMN IF NOT EXISTS account_type VARCHAR(20) DEFAULT 'user';
ALTER TABLE users ADD COLUMN IF NOT EXISTS username VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS avatar_url TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS email VARCHAR(255);
ALTER TABLE users ADD COLUMN IF NOT EXISTS discord_id BIGINT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'free';
ALTER TABLE users ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;
CREATE INDEX IF NOT EXISTS idx_users_username ON users(username);
CREATE INDEX IF NOT EXISTS idx_users_discord_id ON users(discord_id);

CREATE TABLE IF NOT EXISTS portfolio (
    user_id TEXT PRIMARY KEY,
    starting_balance INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS usersettings (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) UNIQUE NOT NULL,
    default_platform VARCHAR(50) DEFAULT 'Console',
    custom_tags JSONB DEFAULT '[]',
    currency_format VARCHAR(20) DEFAULT 'coins',
    theme VARCHAR(20) DEFAULT 'dark',
    timezone VARCHAR(50) DEFAULT 'UTC',
    date_format VARCHAR(10) DEFAULT 'US',
    include_tax_in_profit BOOLEAN DEFAULT true,
    default_chart_range VARCHAR(10) DEFAULT '30d',
    visible_widgets JSONB DEFAULT '["profit", "tax", "balance", "trades"]',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_profiles (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(255),
    avatar_url TEXT,
    global_name VARCHAR(255),
    bio TEXT,
    header_image_url TEXT,
    location VARCHAR(255),
    website_url TEXT,
    twitter_url TEXT,
    youtube_url TEXT,
    twitch_url TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS is_premium BOOLEAN DEFAULT FALSE;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS premium_until TIMESTAMP WITH TIME ZONE;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS bio TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS header_image_url TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS location VARCHAR(255);
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS website_url TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS twitter_url TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS youtube_url TEXT;
ALTER TABLE user_profiles ADD COLUMN IF NOT EXISTS twitch_url TEXT;

-- Billing tables
CREATE TABLE IF NOT EXISTS subscriptions (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL REFERENCES user_profiles(user_id),
    stripe_subscription_id VARCHAR(255) UNIQUE,
    stripe_customer_id VARCHAR(255),
    status VARCHAR(50) NOT NULL DEFAULT 'active',
    plan_id VARCHAR(255) NOT NULL,
    current_period_start TIMESTAMP WITH TIME ZONE,
    current_period_end TIMESTAMP WITH TIME ZONE,
    cancel_at_period_end BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS payments (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    subscription_id INTEGER REFERENCES subscriptions(id),
    stripe_payment_intent_id VARCHAR(255),
    amount INTEGER NOT NULL,
    currency VARCHAR(3) DEFAULT 'GBP',
    status VARCHAR(50) NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);
CREATE TABLE IF NOT EXISTS discord_roles (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    discord_user_id VARCHAR(255) NOT NULL,
    role_id VARCHAR(255) NOT NULL,
    assigned_at TIMESTAMP DEFAULT NOW(),
    expires_at TIMESTAMP
);

-- Public/paid historical-data API keys (app/routers/api_keys.py,
-- app/routers/public_api.py) - only the SHA-256 hash is stored, the
-- plaintext key is shown to the user exactly once at creation time.
CREATE TABLE IF NOT EXISTS api_keys (
    id BIGSERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    name TEXT,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    rate_limit_per_minute INT NOT NULL DEFAULT 60,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_api_keys_user ON api_keys(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_api_keys_hash ON api_keys(key_hash);

CREATE TABLE IF NOT EXISTS trading_goals (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    title VARCHAR(255) NOT NULL,
    target_amount INTEGER NOT NULL,
    target_date DATE,
    goal_type VARCHAR(50) DEFAULT 'profit',
    is_completed BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

-- Backfill trade_id if NULL (compat). Idempotent: only touches rows still
-- missing a trade_id, so it converges to a no-op once applied once.
WITH to_fix AS (
  SELECT ctid, user_id,
         ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY timestamp, player) AS rn
  FROM trades
  WHERE trade_id IS NULL
)
UPDATE trades t
   SET trade_id = ((EXTRACT(EPOCH FROM NOW())*1000)::bigint) + tf.rn
FROM to_fix tf
WHERE t.ctid = tf.ctid AND t.trade_id IS NULL;

-- fut_trades raw ingest
CREATE TABLE IF NOT EXISTS fut_trades (
  id           BIGSERIAL PRIMARY KEY,
  discord_id   TEXT NOT NULL,
  trade_id     BIGINT NOT NULL,
  player_name  TEXT NOT NULL,
  card_version TEXT,
  buy_price    INTEGER,
  sell_price   INTEGER NOT NULL,
  ts           TIMESTAMPTZ NOT NULL,
  source       TEXT DEFAULT 'webapp'
);
CREATE UNIQUE INDEX IF NOT EXISTS fut_trades_uidx ON fut_trades (discord_id, trade_id);

-- events (Next Promo)
CREATE TABLE IF NOT EXISTS events (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,
  start_at TIMESTAMPTZ NOT NULL,
  end_at TIMESTAMPTZ,
  confidence TEXT NOT NULL DEFAULT 'heuristic',
  source TEXT NOT NULL DEFAULT 'rule:18:00',
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_at);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind);

-- Smart Buy tables
CREATE TABLE IF NOT EXISTS smart_buy_suggestions (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    suggestion_type VARCHAR(50) NOT NULL,
    current_price INTEGER NOT NULL,
    target_price INTEGER NOT NULL,
    expected_profit INTEGER NOT NULL,
    risk_level VARCHAR(20) NOT NULL,
    confidence_score INTEGER NOT NULL,
    priority_score INTEGER NOT NULL,
    reasoning TEXT NOT NULL,
    time_to_profit VARCHAR(50),
    platform VARCHAR(10) NOT NULL,
    market_state VARCHAR(30) NOT NULL,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    expires_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_user_created ON smart_buy_suggestions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_card_platform ON smart_buy_suggestions(card_id, platform);

CREATE TABLE IF NOT EXISTS smart_buy_feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    action VARCHAR(20) NOT NULL,
    notes TEXT,
    actual_buy_price INTEGER,
    actual_sell_price INTEGER,
    actual_profit INTEGER,
    timestamp TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_user_action ON smart_buy_feedback(user_id, action);
CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_card ON smart_buy_feedback(card_id);

CREATE TABLE IF NOT EXISTS market_states (
    id BIGSERIAL PRIMARY KEY,
    platform VARCHAR(10) NOT NULL,
    state VARCHAR(30) NOT NULL,
    confidence_score INTEGER NOT NULL,
    detected_at TIMESTAMPTZ DEFAULT NOW(),
    indicators JSONB
);
CREATE INDEX IF NOT EXISTS idx_market_states_platform_detected ON market_states(platform, detected_at DESC);

CREATE TABLE IF NOT EXISTS smart_buy_market_cache (
    id SMALLINT PRIMARY KEY DEFAULT 1,
    payload JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
