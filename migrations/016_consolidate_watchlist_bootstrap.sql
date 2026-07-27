-- Migration 016: consolidate main.py's inline watchlist-DB bootstrap
-- target: watchlist
--
-- Same consolidation as migration 015, for the tables main.py's lifespan()
-- used to create inline on the separate watchlist_pool (WATCHLIST_DATABASE_URL).
-- Moved here verbatim; every statement is IF NOT EXISTS / ADD COLUMN IF NOT
-- EXISTS, so this is a true no-op against a database where these tables
-- already exist.

CREATE TABLE IF NOT EXISTS watchlist (
  id SERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  card_id BIGINT NOT NULL,
  player_name TEXT NOT NULL,
  version TEXT,
  platform TEXT NOT NULL,
  started_price INTEGER NOT NULL,
  started_at TIMESTAMP NOT NULL DEFAULT NOW(),
  last_price INTEGER,
  last_checked TIMESTAMP,
  notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist(user_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_watchlist_unique
ON watchlist(user_id, card_id, platform);

CREATE TABLE IF NOT EXISTS watchlist_alerts (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  user_discord_id TEXT,
  card_id BIGINT NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('ps','xbox','pc')),
  metric TEXT NOT NULL DEFAULT 'price' CHECK (metric IN ('price','liquidity')),
  ref_mode TEXT NOT NULL DEFAULT 'last_close',
  ref_price NUMERIC,
  rise_pct NUMERIC DEFAULT 5,
  fall_pct NUMERIC DEFAULT 5,
  cooloff_minutes INT NOT NULL DEFAULT 30,
  quiet_start TIME,
  quiet_end TIME,
  prefer_dm BOOLEAN NOT NULL DEFAULT TRUE,
  fallback_channel_id TEXT,
  last_alert_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Additive - table may already exist from before liquidity alerts existed.
ALTER TABLE watchlist_alerts
ADD COLUMN IF NOT EXISTS metric TEXT NOT NULL DEFAULT 'price';
CREATE INDEX IF NOT EXISTS idx_alerts_user ON watchlist_alerts(user_id);
CREATE INDEX IF NOT EXISTS idx_alerts_pair ON watchlist_alerts(card_id, platform);
-- New name (not a rename of idx_alerts_pair above) so this is created
-- fresh even on a DB where the table/old index already existed before
-- the metric column was added.
CREATE INDEX IF NOT EXISTS idx_alerts_pair_metric ON watchlist_alerts(card_id, platform, metric);

CREATE TABLE IF NOT EXISTS alerts_log (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  user_discord_id TEXT,
  card_id BIGINT NOT NULL,
  platform TEXT NOT NULL,
  direction TEXT NOT NULL,
  pct NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  ref_mode TEXT NOT NULL,
  ref_price NUMERIC,
  sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts_log(user_id, sent_at);
