-- Migration: Monetization Features (Referrals, Alerts, Portfolio Snapshots)
-- Date: 2024-12-07

-- Referral codes table
CREATE TABLE IF NOT EXISTS referral_codes (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_codes_code ON referral_codes(code);
CREATE INDEX IF NOT EXISTS idx_referral_codes_user ON referral_codes(user_id);

-- Referral clicks tracking
CREATE TABLE IF NOT EXISTS referral_clicks (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_clicks_code ON referral_clicks(code);
CREATE INDEX IF NOT EXISTS idx_referral_clicks_time ON referral_clicks(clicked_at);

-- Add referred_by column to users table (if it doesn't exist)
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'referred_by'
    ) THEN
        ALTER TABLE users ADD COLUMN referred_by TEXT;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);

-- Trade alerts table
CREATE TABLE IF NOT EXISTS trade_alerts (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    player_card_id TEXT NOT NULL,
    alert_type TEXT NOT NULL, -- 'price_drop', 'price_rise', 'volume_spike'
    trigger_condition JSONB NOT NULL, -- {target_price, threshold, etc}
    is_active BOOLEAN NOT NULL DEFAULT true,
    last_triggered_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_trade_alerts_user ON trade_alerts(user_id);
CREATE INDEX IF NOT EXISTS idx_trade_alerts_active ON trade_alerts(is_active) WHERE is_active = true;
CREATE INDEX IF NOT EXISTS idx_trade_alerts_player ON trade_alerts(player_card_id);

-- Portfolio snapshots table (for historical tracking)
CREATE TABLE IF NOT EXISTS portfolio_snapshots (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    snapshot_date DATE NOT NULL,
    total_profit INTEGER NOT NULL DEFAULT 0,
    total_trades INTEGER NOT NULL DEFAULT 0,
    win_rate DECIMAL(5,4),
    total_invested INTEGER NOT NULL DEFAULT 0,
    roi DECIMAL(8,4),
    unique_players INTEGER NOT NULL DEFAULT 0,
    metrics JSONB, -- Additional metrics like sharpe_ratio, max_drawdown, etc.
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_portfolio_snapshots_user_date ON portfolio_snapshots(user_id, snapshot_date);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_user ON portfolio_snapshots(user_id);
CREATE INDEX IF NOT EXISTS idx_portfolio_snapshots_date ON portfolio_snapshots(snapshot_date);

-- Add tier column to users table for 3-tier system
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'tier'
    ) THEN
        ALTER TABLE users ADD COLUMN tier TEXT DEFAULT 'basic';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_users_tier ON users(tier);

-- Add privacy settings for leaderboard
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'users' AND column_name = 'privacy_settings'
    ) THEN
        ALTER TABLE users ADD COLUMN privacy_settings JSONB DEFAULT '{"show_on_leaderboard": true}';
    END IF;
END $$;
