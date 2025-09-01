
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
CREATE INDEX IF NOT EXISTS idx_events_start ON events (start_at);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events (kind);

CREATE TABLE IF NOT EXISTS watchlist_items (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  user_discord_id TEXT,
  player_id BIGINT NOT NULL,
  platform TEXT NOT NULL CHECK (platform IN ('ps','xbox','pc')),
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
CREATE INDEX IF NOT EXISTS idx_watchlist_player_platform ON watchlist_items (player_id, platform);
CREATE INDEX IF NOT EXISTS idx_watchlist_user ON watchlist_items (user_id);

CREATE TABLE IF NOT EXISTS alerts_log (
  id BIGSERIAL PRIMARY KEY,
  user_id TEXT NOT NULL,
  user_discord_id TEXT,
  player_id BIGINT NOT NULL,
  platform TEXT NOT NULL,
  direction TEXT NOT NULL,
  pct NUMERIC NOT NULL,
  price NUMERIC NOT NULL,
  ref_mode TEXT NOT NULL,
  ref_price NUMERIC,
  sent_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_alerts_user_time ON alerts_log (user_id, sent_at);
