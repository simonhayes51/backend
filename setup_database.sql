-- ============================================
-- FutHub Backend - Complete Database Setup
-- Run this script in pgAdmin Query Tool
-- ============================================

-- 1. TRADES TABLE (Core)
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
CREATE INDEX IF NOT EXISTS idx_trades_user_ts ON trades(user_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_trades_tag ON trades(user_id, tag);
CREATE INDEX IF NOT EXISTS idx_trades_platform ON trades(user_id, platform);

-- 2. USERS TABLE (Entitlements)
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    plan TEXT,
    premium_until TIMESTAMPTZ,
    roles JSONB DEFAULT '[]',
    tier TEXT DEFAULT 'basic',
    referred_by TEXT,
    privacy_settings JSONB DEFAULT '{"show_on_leaderboard": true}'
);

CREATE INDEX IF NOT EXISTS idx_users_tier ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);

-- 3. PORTFOLIO TABLE
CREATE TABLE IF NOT EXISTS portfolio (
    user_id TEXT PRIMARY KEY,
    starting_balance INTEGER NOT NULL DEFAULT 0
);

-- 4. USER SETTINGS TABLE
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
    created_at TIMESTAMPTZ DEFAULT NOW(),
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 5. REFERRAL CODES TABLE
CREATE TABLE IF NOT EXISTS referral_codes (
    id SERIAL PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE,
    code TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_codes_code ON referral_codes(code);
CREATE INDEX IF NOT EXISTS idx_referral_codes_user ON referral_codes(user_id);

-- 6. REFERRAL CLICKS TABLE
CREATE TABLE IF NOT EXISTS referral_clicks (
    id SERIAL PRIMARY KEY,
    code TEXT NOT NULL,
    ip_address TEXT,
    user_agent TEXT,
    clicked_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_referral_clicks_code ON referral_clicks(code);
CREATE INDEX IF NOT EXISTS idx_referral_clicks_time ON referral_clicks(clicked_at);

-- 7. TRADE ALERTS TABLE
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

-- 8. PORTFOLIO SNAPSHOTS TABLE
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

-- ============================================
-- VERIFICATION QUERIES
-- Run these to verify tables were created
-- ============================================

-- Check all tables exist
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN (
    'trades',
    'users',
    'portfolio',
    'usersettings',
    'referral_codes',
    'referral_clicks',
    'trade_alerts',
    'portfolio_snapshots'
  )
ORDER BY table_name;

-- Check trades table structure
SELECT column_name, data_type, is_nullable
FROM information_schema.columns
WHERE table_name = 'trades'
ORDER BY ordinal_position;

-- Check indexes
SELECT indexname, tablename
FROM pg_indexes
WHERE tablename IN ('trades', 'users', 'referral_codes', 'referral_clicks', 'trade_alerts', 'portfolio_snapshots')
ORDER BY tablename, indexname;
