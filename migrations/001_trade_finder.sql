-- migrations/001_trade_finder.sql

-- Cache table
CREATE TABLE IF NOT EXISTS trade_finder_cache (
  cache_key TEXT PRIMARY KEY,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_finder_cache_created ON trade_finder_cache (created_at);

-- Alerts config (future autoposts)
CREATE TABLE IF NOT EXISTS trade_alerts (
  id SERIAL PRIMARY KEY,
  owner_id TEXT,
  owner_type TEXT CHECK (owner_type IN ('user','guild')) DEFAULT 'guild',
  channel_id TEXT,
  ping_role_id TEXT,
  is_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  frequency_hours INT NOT NULL DEFAULT 6,
  timeframe_hours INT NOT NULL DEFAULT 24,
  platform TEXT CHECK (platform IN ('console','pc')) NOT NULL DEFAULT 'console',
  min_profit INT NOT NULL DEFAULT 1500,
  min_margin_pct NUMERIC NOT NULL DEFAULT 8,
  budget_max INT NOT NULL DEFAULT 100000,
  rating_min INT NOT NULL DEFAULT 75,
  rating_max INT NOT NULL DEFAULT 93,
  includes JSONB DEFAULT '{}'::jsonb,
  last_run_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_trade_alerts_enabled ON trade_alerts (is_enabled);

-- Rewards/day market windows
CREATE TABLE IF NOT EXISTS market_windows (
  id SERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  dow INT NOT NULL,              -- 0=Mon..6=Sun (UTC)
  start_hour_utc INT NOT NULL,   -- 0-23
  duration_hours INT NOT NULL DEFAULT 2,
  weight NUMERIC NOT NULL DEFAULT 1.0,
  active BOOLEAN NOT NULL DEFAULT TRUE
);

-- Example seeds (adjust per season; ON CONFLICT is skipped for simplicity)
INSERT INTO market_windows (name, dow, start_hour_utc, duration_hours, weight, active)
SELECT 'Rivals Rewards', 4, 6, 3, 1.0, TRUE
WHERE NOT EXISTS (SELECT 1 FROM market_windows WHERE name='Rivals Rewards' AND dow=4);

INSERT INTO market_windows (name, dow, start_hour_utc, duration_hours, weight, active)
SELECT 'WL Rewards', 0, 7, 3, 1.0, TRUE
WHERE NOT EXISTS (SELECT 1 FROM market_windows WHERE name='WL Rewards' AND dow=0);
