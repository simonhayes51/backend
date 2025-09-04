-- migrations/002_smart_buy.sql
-- Smart Buy feature database schema

-- Smart Buy suggestions cache table
CREATE TABLE IF NOT EXISTS smart_buy_suggestions (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    suggestion_type VARCHAR(50) NOT NULL, -- 'crash_anticipation', 'recovery_play', etc.
    current_price INTEGER NOT NULL,
    target_price INTEGER NOT NULL,
    expected_profit INTEGER NOT NULL,
    risk_level VARCHAR(20) NOT NULL, -- 'low', 'medium', 'high'
    confidence_score INTEGER NOT NULL, -- 0-100
    priority_score INTEGER NOT NULL, -- 1-10
    reasoning TEXT NOT NULL,
    time_to_profit VARCHAR(50),
    platform VARCHAR(10) NOT NULL,
    market_state VARCHAR(30) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    expires_at TIMESTAMP WITH TIME ZONE,
    INDEX (user_id, created_at),
    INDEX (card_id, platform),
    INDEX (expires_at)
);

-- User feedback on suggestions for ML training
CREATE TABLE IF NOT EXISTS smart_buy_feedback (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    action VARCHAR(20) NOT NULL, -- 'bought', 'ignored', 'watchlisted'
    notes TEXT,
    actual_buy_price INTEGER,
    actual_sell_price INTEGER,
    actual_profit INTEGER,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    suggestion_id BIGINT REFERENCES smart_buy_suggestions(id),
    INDEX (user_id, action),
    INDEX (card_id, action),
    INDEX (timestamp)
);

-- Market state tracking for historical analysis
CREATE TABLE IF NOT EXISTS market_states (
    id BIGSERIAL PRIMARY KEY,
    platform VARCHAR(10) NOT NULL,
    state VARCHAR(30) NOT NULL, -- 'normal', 'pre_crash', 'crash_active', etc.
    confidence_score INTEGER NOT NULL,
    detected_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    indicators JSONB, -- Store various market indicators
    INDEX (platform, detected_at),
    INDEX (state, detected_at)
);

-- Upcoming events that might affect market
CREATE TABLE IF NOT EXISTS market_events (
    id BIGSERIAL PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    event_type VARCHAR(50) NOT NULL, -- 'promo', 'sbc', 'totw', 'rewards', etc.
    expected_date TIMESTAMP WITH TIME ZONE,
    actual_date TIMESTAMP WITH TIME ZONE,
    impact_level VARCHAR(20), -- 'low', 'medium', 'high'
    affected_categories JSONB, -- Array of categories that might be affected
    description TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    INDEX (expected_date),
    INDEX (event_type),
    INDEX (impact_level)
);

-- User preferences for smart buy suggestions
CREATE TABLE IF NOT EXISTS smart_buy_preferences (
    id BIGSERIAL PRIMARY KEY,
    user_id TEXT UNIQUE NOT NULL,
    default_budget INTEGER DEFAULT 100000,
    risk_tolerance VARCHAR(20) DEFAULT 'moderate', -- 'conservative', 'moderate', 'aggressive'
    preferred_time_horizon VARCHAR(20) DEFAULT 'short', -- 'quick_flip', 'short', 'long_term'
    preferred_categories JSONB DEFAULT '[]'::jsonb,
    excluded_positions JSONB DEFAULT '[]'::jsonb,
    preferred_leagues JSONB DEFAULT '[]'::jsonb,
    preferred_nations JSONB DEFAULT '[]'::jsonb,
    min_rating INTEGER DEFAULT 75,
    max_rating INTEGER DEFAULT 95,
    min_profit INTEGER DEFAULT 1000,
    notifications_enabled BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    INDEX (user_id)
);

-- Performance tracking for suggestions
CREATE TABLE IF NOT EXISTS suggestion_performance (
    id BIGSERIAL PRIMARY KEY,
    suggestion_id BIGINT REFERENCES smart_buy_suggestions(id),
    user_id TEXT NOT NULL,
    card_id TEXT NOT NULL,
    suggested_price INTEGER NOT NULL,
    target_price INTEGER NOT NULL,
    actual_buy_price INTEGER,
    actual_sell_price INTEGER,
    actual_profit INTEGER,
    time_to_buy_hours INTEGER, -- How long before user bought
    time_to_sell_hours INTEGER, -- How long to sell after buying
    success BOOLEAN, -- Whether target was achieved
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    INDEX (user_id, success),
    INDEX (card_id),
    INDEX (created_at)
);

-- Seed some initial market events (adjust dates as needed)
INSERT INTO market_events (name, event_type, expected_date, impact_level, affected_categories, description)
VALUES 
    ('Weekend League Rewards', 'rewards', NOW() + INTERVAL '3 days', 'medium', '["all"]', 'Weekly rewards distribution increases market supply'),
    ('TOTW Release', 'promo', NOW() + INTERVAL '1 week', 'high', '["if_cards", "totw"]', 'Team of the Week card releases'),
    ('Daily Content Drop', 'content', NOW() + INTERVAL '1 day', 'low', '["all"]', 'Daily 6PM UK content release'),
    ('Rivals Rewards', 'rewards', NOW() + INTERVAL '4 days', 'medium', '["all"]', 'Division Rivals rewards distribution')
ON CONFLICT DO NOTHING;

-- Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_user_created ON smart_buy_suggestions(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_smart_buy_suggestions_expires ON smart_buy_suggestions(expires_at) WHERE expires_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_smart_buy_feedback_user_timestamp ON smart_buy_feedback(user_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_market_states_platform_time ON market_states(platform, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_suggestion_performance_user_success ON suggestion_performance(user_id, success);

-- Add cleanup function for expired suggestions
CREATE OR REPLACE FUNCTION cleanup_expired_suggestions()
RETURNS void AS $
BEGIN
    DELETE FROM smart_buy_suggestions 
    WHERE expires_at IS NOT NULL AND expires_at < NOW() - INTERVAL '1 day';
END;
$ LANGUAGE plpgsql;
