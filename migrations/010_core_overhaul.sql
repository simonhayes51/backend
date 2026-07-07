-- Migration 010: Overhaul — run against the CORE database (DATABASE_URL).
--
-- 1. Stripe webhook idempotency
-- 2. Public Data API v2: key tiers, monthly quotas, usage metering
-- 3. Pipeline heartbeats (written by auto_sync workers, read by /api/ops/freshness)

-- ============================================================
-- 1. Stripe webhook idempotency
-- ============================================================
CREATE TABLE IF NOT EXISTS stripe_events (
    event_id   TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================
-- 2. API v2 key tiers + quotas + metering
-- ============================================================
-- Tiers: starter (free taster), trader (paid), dev (paid, high volume)
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS tier TEXT NOT NULL DEFAULT 'starter';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS monthly_quota INTEGER NOT NULL DEFAULT 5000;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS quota_period_start DATE NOT NULL DEFAULT date_trunc('month', now())::date;

-- One row per key per UTC day; cheap to upsert, easy to bill from.
CREATE TABLE IF NOT EXISTS api_key_usage (
    api_key_id BIGINT NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    day        DATE   NOT NULL,
    requests   BIGINT NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, day)
);
CREATE INDEX IF NOT EXISTS idx_api_key_usage_day ON api_key_usage(day);

-- ============================================================
-- 3. Pipeline heartbeats
-- ============================================================
CREATE TABLE IF NOT EXISTS pipeline_heartbeats (
    worker      TEXT PRIMARY KEY,          -- e.g. 'futbin_full_sync', 'ea_price_sync', 'bin_sales_history_sync'
    last_run_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ok          BOOLEAN NOT NULL DEFAULT TRUE,
    detail      TEXT
);
