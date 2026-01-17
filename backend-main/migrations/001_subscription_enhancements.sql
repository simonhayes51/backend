-- Migration: Subscription System Enhancements for OnlyFans-style features
-- Date: 2026-01-12
-- Description: Add tier-based subscriptions, tips, saved posts, and content requests

-- ============================================================================
-- 1. Update trader_subscriptions table with new fields
-- ============================================================================

-- Add new columns if they don't exist
DO $$ 
BEGIN
    -- Add price_locked column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_subscriptions' 
        AND column_name = 'price_locked'
    ) THEN
        ALTER TABLE trader_subscriptions 
        ADD COLUMN price_locked DECIMAL(10,2) DEFAULT 0;
    END IF;

    -- Add is_founding_subscriber column
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_subscriptions' 
        AND column_name = 'is_founding_subscriber'
    ) THEN
        ALTER TABLE trader_subscriptions 
        ADD COLUMN is_founding_subscriber BOOLEAN DEFAULT FALSE;
    END IF;

    -- Update subscription_type to support tiers
    IF EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_subscriptions' 
        AND column_name = 'subscription_type'
        AND data_type = 'character varying'
    ) THEN
        -- Already a string type, just add check constraint if needed
        ALTER TABLE trader_subscriptions 
        DROP CONSTRAINT IF EXISTS subscription_type_check;
        
        ALTER TABLE trader_subscriptions 
        ADD CONSTRAINT subscription_type_check 
        CHECK (subscription_type IN ('free', 'basic', 'premium', 'elite'));
    END IF;
END $$;

-- ============================================================================
-- 2. Create post_tips table for tipping/boosting posts
-- ============================================================================

CREATE TABLE IF NOT EXISTS post_tips (
    id SERIAL PRIMARY KEY,
    post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    from_user_id VARCHAR(255) NOT NULL,
    to_user_id VARCHAR(255) NOT NULL,
    amount DECIMAL(10,2) NOT NULL CHECK (amount > 0 AND amount <= 100),
    created_at TIMESTAMP DEFAULT NOW(),
    processed BOOLEAN DEFAULT FALSE,
    
    CONSTRAINT no_self_tip CHECK (from_user_id != to_user_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_post_tips_post_id ON post_tips(post_id);
CREATE INDEX IF NOT EXISTS idx_post_tips_to_user ON post_tips(to_user_id);
CREATE INDEX IF NOT EXISTS idx_post_tips_from_user ON post_tips(from_user_id);
CREATE INDEX IF NOT EXISTS idx_post_tips_created_at ON post_tips(created_at);

-- ============================================================================
-- 3. Create saved_posts table for user's personal library
-- ============================================================================

CREATE TABLE IF NOT EXISTS saved_posts (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) NOT NULL,
    post_id INTEGER NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    saved_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(user_id, post_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_saved_posts_user_id ON saved_posts(user_id);
CREATE INDEX IF NOT EXISTS idx_saved_posts_post_id ON saved_posts(post_id);
CREATE INDEX IF NOT EXISTS idx_saved_posts_saved_at ON saved_posts(saved_at DESC);

-- ============================================================================
-- 4. Create content_requests table for subscriber content requests
-- ============================================================================

CREATE TABLE IF NOT EXISTS content_requests (
    id SERIAL PRIMARY KEY,
    trader_id VARCHAR(255) NOT NULL,
    requester_id VARCHAR(255) NOT NULL,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    category VARCHAR(100),
    upvotes INTEGER DEFAULT 0,
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'declined')),
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    completed_at TIMESTAMP,
    completed_post_id INTEGER REFERENCES social_posts(id) ON DELETE SET NULL
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_content_requests_trader_id ON content_requests(trader_id);
CREATE INDEX IF NOT EXISTS idx_content_requests_status ON content_requests(status);
CREATE INDEX IF NOT EXISTS idx_content_requests_upvotes ON content_requests(upvotes DESC);
CREATE INDEX IF NOT EXISTS idx_content_requests_created_at ON content_requests(created_at DESC);

-- ============================================================================
-- 5. Create content_request_votes table for upvoting requests
-- ============================================================================

CREATE TABLE IF NOT EXISTS content_request_votes (
    id SERIAL PRIMARY KEY,
    request_id INTEGER NOT NULL REFERENCES content_requests(id) ON DELETE CASCADE,
    user_id VARCHAR(255) NOT NULL,
    voted_at TIMESTAMP DEFAULT NOW(),
    
    UNIQUE(request_id, user_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_content_request_votes_request_id ON content_request_votes(request_id);
CREATE INDEX IF NOT EXISTS idx_content_request_votes_user_id ON content_request_votes(user_id);

-- ============================================================================
-- 6. Create trigger to update content_requests upvotes count
-- ============================================================================

CREATE OR REPLACE FUNCTION update_content_request_upvotes()
RETURNS TRIGGER AS $$
BEGIN
    IF (TG_OP = 'INSERT') THEN
        UPDATE content_requests 
        SET upvotes = upvotes + 1, updated_at = NOW()
        WHERE id = NEW.request_id;
    ELSIF (TG_OP = 'DELETE') THEN
        UPDATE content_requests 
        SET upvotes = GREATEST(0, upvotes - 1), updated_at = NOW()
        WHERE id = OLD.request_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS content_request_vote_count ON content_request_votes;
CREATE TRIGGER content_request_vote_count
    AFTER INSERT OR DELETE ON content_request_votes
    FOR EACH ROW
    EXECUTE FUNCTION update_content_request_upvotes();

-- ============================================================================
-- 7. Add trader profile tier pricing columns
-- ============================================================================

DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'tier_basic_price'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN tier_basic_price DECIMAL(10,2) DEFAULT 4.99;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'tier_premium_price'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN tier_premium_price DECIMAL(10,2) DEFAULT 9.99;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'tier_elite_price'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN tier_elite_price DECIMAL(10,2) DEFAULT 19.99;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'tier_basic_cap'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN tier_basic_cap INTEGER;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'tier_premium_cap'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN tier_premium_cap INTEGER;
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'tier_elite_cap'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN tier_elite_cap INTEGER;
    END IF;
END $$;

-- ============================================================================
-- 8. Add status/mood columns to trader_profiles
-- ============================================================================

DO $$ 
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'current_status'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN current_status VARCHAR(500);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns 
        WHERE table_name = 'trader_profiles' 
        AND column_name = 'status_updated_at'
    ) THEN
        ALTER TABLE trader_profiles 
        ADD COLUMN status_updated_at TIMESTAMP;
    END IF;
END $$;

-- ============================================================================
-- DONE - Migration complete
-- ============================================================================
