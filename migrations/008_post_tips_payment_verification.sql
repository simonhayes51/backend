-- Migration: Require verified payment before recording post tips
-- Date: 2026-07-04
-- Description: post_tips previously had no link to an actual payment record,
-- which let /api/subscriptions/tip fabricate "tips" with no money changing
-- hands. Add a column to store the Stripe PaymentIntent that paid for the
-- tip so the row can only be inserted after a verified Stripe Checkout
-- Session confirms payment, and so repeated confirmation calls are
-- idempotent instead of double-crediting.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'post_tips'
        AND column_name = 'stripe_payment_intent_id'
    ) THEN
        ALTER TABLE post_tips
        ADD COLUMN stripe_payment_intent_id VARCHAR(255);
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS idx_post_tips_stripe_payment_intent
ON post_tips(stripe_payment_intent_id)
WHERE stripe_payment_intent_id IS NOT NULL;

COMMENT ON COLUMN post_tips.stripe_payment_intent_id IS 'Stripe PaymentIntent ID that paid for this tip; used to verify payment and prevent duplicate inserts on repeated confirmation calls';
COMMENT ON COLUMN post_tips.processed IS 'TRUE once the tip payment has been verified with Stripe and recorded';

-- ============================================================================
-- DONE - Migration complete
-- ============================================================================
