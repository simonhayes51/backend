-- Run this migration against DATABASE_URL (the database containing trades) only.
--
-- Migration 032 added recommendation_* columns that snapshot the verdict
-- at the moment a trade was opened. Those never change afterward - they
-- are what recommendation_engine_v2.py said THEN. This adds a second,
-- separately-refreshed set of columns for what the engine says NOW about
-- a card the user still holds, computed by app/services/
-- held_position_refresher.py calling evaluate_card(..., is_held=True,
-- held_purchase_price=buy) on a recurring pass - the engine's
-- _evaluate_held_position/held_purchase_price path already exists and
-- already returns a held_decision (KEEP/SELL/etc) + reasons, it has just
-- never had a real caller until now.
BEGIN;

ALTER TABLE trades ADD COLUMN IF NOT EXISTS current_recommendation_status TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS current_recommendation_reasoning TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS current_evaluated_at TIMESTAMPTZ;

COMMIT;
