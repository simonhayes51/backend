-- ============================================
-- FutHub Backend - Database Migration
-- Add missing columns to existing tables
-- Run this in pgAdmin after the main setup
-- ============================================

-- Add new columns to users table for monetization features
ALTER TABLE users ADD COLUMN IF NOT EXISTS tier TEXT DEFAULT 'basic';
ALTER TABLE users ADD COLUMN IF NOT EXISTS referred_by TEXT;
ALTER TABLE users ADD COLUMN IF NOT EXISTS privacy_settings JSONB DEFAULT '{"show_on_leaderboard": true}';

-- Create indexes for new columns
CREATE INDEX IF NOT EXISTS idx_users_tier ON users(tier);
CREATE INDEX IF NOT EXISTS idx_users_referred_by ON users(referred_by);

-- Verify the changes
SELECT column_name, data_type, column_default
FROM information_schema.columns
WHERE table_name = 'users'
ORDER BY ordinal_position;

-- Show count of users by tier
SELECT tier, COUNT(*) as count
FROM users
GROUP BY tier;
