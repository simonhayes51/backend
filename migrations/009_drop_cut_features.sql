-- Drops tables belonging to features removed from the codebase in this
-- pass: SBC Hub, Social Feed, Messaging, Trader Marketplace (profiles,
-- ratings, admin approval), Tipping/Content Purchases, and the PayPal/
-- Stripe-Connect trader payout rails. Referrals, the trading-performance
-- leaderboard, and personal trade logging are unaffected and keep their
-- tables (referral_codes/referral_clicks, trades, subscriptions/payments
-- for the app's own Premium billing).
--
-- Run this manually against the production Postgres instance - the app
-- has no live users yet, so this is safe, but it is a destructive,
-- irreversible operation and is deliberately not run automatically.

BEGIN;

DROP TABLE IF EXISTS trader_payouts CASCADE;
DROP TABLE IF EXISTS trader_earnings CASCADE;
DROP TABLE IF EXISTS trader_ratings CASCADE;
DROP TABLE IF EXISTS trader_subscriptions CASCADE;
DROP TABLE IF EXISTS content_request_votes CASCADE;
DROP TABLE IF EXISTS content_requests CASCADE;
DROP TABLE IF EXISTS content_purchases CASCADE;
DROP TABLE IF EXISTS saved_posts CASCADE;
DROP TABLE IF EXISTS post_tips CASCADE;
DROP TABLE IF EXISTS comment_likes CASCADE;
DROP TABLE IF EXISTS post_reactions CASCADE;
DROP TABLE IF EXISTS post_comments CASCADE;
DROP TABLE IF EXISTS social_posts CASCADE;
DROP TABLE IF EXISTS notifications CASCADE;
DROP TABLE IF EXISTS messages CASCADE;
DROP TABLE IF EXISTS conversations CASCADE;
DROP TABLE IF EXISTS trader_profiles CASCADE;

-- SBC tables, if they exist in this database (created by the ingest
-- pipeline that shipped with routes_ingest_sets.py / ea_sbc_*_ingest.py).
DROP TABLE IF EXISTS sbc_challenges CASCADE;
DROP TABLE IF EXISTS sbc_sets CASCADE;

COMMIT;
