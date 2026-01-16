-- Migration: Single-Tier Subscriptions and One-Off Content Pricing
-- Date: 2026-01-16
-- Description: Simplify subscription system to single tier per trader and add one-off content purchases

-- ============================================================================
-- 1. Update trader_profiles to use single subscription price
-- ============================================================================

-- The subscription_price column already exists from migration 003
-- We'll keep the tier columns for backward compatibility but deprecate their use
-- in favor of the single subscription_price field

DO $$
BEGIN
    -- Ensure subscription_price column exists
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'subscription_price'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN subscription_price DECIMAL(10,2) DEFAULT 0;
    END IF;
END $$;

-- Add comment to clarify usage
COMMENT ON COLUMN trader_profiles.subscription_price IS 'Single monthly subscription price set by trader (replaces multi-tier system)';

-- ============================================================================
-- 2. Update trader_subscriptions to support paid/free model
-- ============================================================================

-- Update subscription_type constraint to support both old and new values during transition
ALTER TABLE trader_subscriptions
DROP CONSTRAINT IF EXISTS subscription_type_check;

ALTER TABLE trader_subscriptions
ADD CONSTRAINT subscription_type_check
CHECK (subscription_type IN ('free', 'paid', 'basic', 'premium', 'elite'));

-- Add columns for PayPal support
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_subscriptions'
        AND column_name = 'paypal_subscription_id'
    ) THEN
        ALTER TABLE trader_subscriptions
        ADD COLUMN paypal_subscription_id VARCHAR(255);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_subscriptions'
        AND column_name = 'payment_provider'
    ) THEN
        ALTER TABLE trader_subscriptions
        ADD COLUMN payment_provider VARCHAR(50) DEFAULT 'stripe' CHECK (payment_provider IN ('stripe', 'paypal'));
    END IF;
END $$;

-- Add index for PayPal subscriptions
CREATE INDEX IF NOT EXISTS idx_trader_subscriptions_paypal ON trader_subscriptions(paypal_subscription_id) WHERE paypal_subscription_id IS NOT NULL;

COMMENT ON COLUMN trader_subscriptions.payment_provider IS 'Payment processor used: stripe or paypal';
COMMENT ON COLUMN trader_subscriptions.paypal_subscription_id IS 'PayPal subscription ID for recurring payments';

-- ============================================================================
-- 3. Add one-off content pricing to social_posts
-- ============================================================================

DO $$
BEGIN
    -- Add price column for one-off content
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'social_posts'
        AND column_name = 'price'
    ) THEN
        ALTER TABLE social_posts
        ADD COLUMN price DECIMAL(10,2) DEFAULT NULL;
    END IF;

    -- Add flag to differentiate between subscriber-only and paid content
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'social_posts'
        AND column_name = 'requires_purchase'
    ) THEN
        ALTER TABLE social_posts
        ADD COLUMN requires_purchase BOOLEAN DEFAULT FALSE;
    END IF;
END $$;

-- Add index for paid content
CREATE INDEX IF NOT EXISTS idx_social_posts_requires_purchase ON social_posts(requires_purchase) WHERE requires_purchase = TRUE;

-- Add check constraint to ensure pricing logic
ALTER TABLE social_posts
DROP CONSTRAINT IF EXISTS social_posts_pricing_check;

ALTER TABLE social_posts
ADD CONSTRAINT social_posts_pricing_check
CHECK (
    (requires_purchase = FALSE AND price IS NULL) OR
    (requires_purchase = TRUE AND price IS NOT NULL AND price > 0)
);

COMMENT ON COLUMN social_posts.price IS 'One-off purchase price for this content (NULL for free/subscriber-only content)';
COMMENT ON COLUMN social_posts.requires_purchase IS 'TRUE if this content requires one-off purchase, FALSE if free or subscriber-only (is_premium)';
COMMENT ON COLUMN social_posts.is_premium IS 'TRUE if content is subscriber-only (does not require purchase, just subscription)';

-- ============================================================================
-- 4. Create content_purchases table for one-off content
-- ============================================================================

CREATE TABLE IF NOT EXISTS content_purchases (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    post_id BIGINT NOT NULL REFERENCES social_posts(id) ON DELETE CASCADE,
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'GBP',

    -- Payment provider details
    payment_provider VARCHAR(50) NOT NULL CHECK (payment_provider IN ('stripe', 'paypal')),
    stripe_payment_intent_id VARCHAR(255),
    paypal_order_id VARCHAR(255),
    paypal_capture_id VARCHAR(255),

    -- Payment status
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN ('pending', 'completed', 'failed', 'refunded')),

    -- Timestamps
    purchased_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    refunded_at TIMESTAMP,

    -- Prevent duplicate purchases
    UNIQUE(user_id, post_id)
);

-- Indexes for performance
CREATE INDEX IF NOT EXISTS idx_content_purchases_user ON content_purchases(user_id);
CREATE INDEX IF NOT EXISTS idx_content_purchases_post ON content_purchases(post_id);
CREATE INDEX IF NOT EXISTS idx_content_purchases_status ON content_purchases(status);
CREATE INDEX IF NOT EXISTS idx_content_purchases_stripe ON content_purchases(stripe_payment_intent_id) WHERE stripe_payment_intent_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_content_purchases_paypal_order ON content_purchases(paypal_order_id) WHERE paypal_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_content_purchases_paypal_capture ON content_purchases(paypal_capture_id) WHERE paypal_capture_id IS NOT NULL;

COMMENT ON TABLE content_purchases IS 'One-off purchases of paid content (signals, guides, etc.)';
COMMENT ON COLUMN content_purchases.payment_provider IS 'Payment processor used: stripe or paypal';

-- ============================================================================
-- 5. Add PayPal support to payments table
-- ============================================================================

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments'
        AND column_name = 'payment_provider'
    ) THEN
        ALTER TABLE payments
        ADD COLUMN payment_provider VARCHAR(50) DEFAULT 'stripe' CHECK (payment_provider IN ('stripe', 'paypal'));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments'
        AND column_name = 'paypal_order_id'
    ) THEN
        ALTER TABLE payments
        ADD COLUMN paypal_order_id VARCHAR(255);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'payments'
        AND column_name = 'paypal_capture_id'
    ) THEN
        ALTER TABLE payments
        ADD COLUMN paypal_capture_id VARCHAR(255);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_payments_paypal_order ON payments(paypal_order_id) WHERE paypal_order_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_payments_paypal_capture ON payments(paypal_capture_id) WHERE paypal_capture_id IS NOT NULL;

-- ============================================================================
-- 6. Update notification types to include content purchase
-- ============================================================================

-- Drop existing constraint
ALTER TABLE notifications
DROP CONSTRAINT IF EXISTS notifications_notification_type_check;

-- Add new constraint with purchase notification type
ALTER TABLE notifications
ADD CONSTRAINT notifications_notification_type_check
CHECK (notification_type IN (
    'new_follower', 'post_like', 'post_comment', 'new_message',
    'new_rating', 'mention', 'subscription', 'post_tip', 'content_purchase'
));

-- ============================================================================
-- 7. Create view for trader earnings analytics
-- ============================================================================

CREATE OR REPLACE VIEW trader_earnings AS
SELECT
    tp.user_id as trader_id,
    up.username,

    -- Subscription earnings (monthly recurring)
    COUNT(DISTINCT ts.id) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type = 'paid') as active_subscribers,
    COALESCE(SUM(ts.amount) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type = 'paid'), 0) as monthly_subscription_revenue,

    -- One-off content sales (lifetime)
    COUNT(DISTINCT cp.id) FILTER (WHERE cp.status = 'completed') as content_sales,
    COALESCE(SUM(cp.amount) FILTER (WHERE cp.status = 'completed'), 0) as total_content_revenue,

    -- Tips (lifetime)
    COALESCE(SUM(pt.amount), 0) as total_tips_received,

    -- Combined metrics
    COALESCE(SUM(ts.amount) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type = 'paid'), 0) as estimated_monthly_revenue
FROM trader_profiles tp
LEFT JOIN user_profiles up ON tp.user_id = up.user_id
LEFT JOIN trader_subscriptions ts ON tp.user_id = ts.trader_id
LEFT JOIN social_posts sp ON tp.user_id = sp.user_id
LEFT JOIN content_purchases cp ON sp.id = cp.post_id
LEFT JOIN post_tips pt ON sp.id = pt.post_id
GROUP BY tp.user_id, up.username;

COMMENT ON VIEW trader_earnings IS 'Analytics view for trader revenue from subscriptions, content sales, and tips';

-- ============================================================================
-- 8. Create function to check content access
-- ============================================================================

CREATE OR REPLACE FUNCTION user_can_access_content(
    p_user_id BIGINT,
    p_post_id BIGINT
) RETURNS BOOLEAN AS $$
DECLARE
    v_post RECORD;
    v_is_author BOOLEAN;
    v_is_subscribed BOOLEAN;
    v_has_purchased BOOLEAN;
BEGIN
    -- Get post details
    SELECT * INTO v_post FROM social_posts WHERE id = p_post_id;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- Check if user is the author
    v_is_author := (v_post.user_id = p_user_id);
    IF v_is_author THEN
        RETURN TRUE;
    END IF;

    -- If content is free (not premium and not requires_purchase)
    IF v_post.is_premium = FALSE AND v_post.requires_purchase = FALSE THEN
        RETURN TRUE;
    END IF;

    -- If content requires purchase, check if purchased
    IF v_post.requires_purchase = TRUE THEN
        SELECT EXISTS(
            SELECT 1 FROM content_purchases
            WHERE user_id = p_user_id
            AND post_id = p_post_id
            AND status = 'completed'
        ) INTO v_has_purchased;

        RETURN v_has_purchased;
    END IF;

    -- If content is premium (subscriber-only), check subscription
    IF v_post.is_premium = TRUE THEN
        SELECT EXISTS(
            SELECT 1 FROM trader_subscriptions
            WHERE subscriber_id = p_user_id
            AND trader_id = v_post.user_id
            AND is_active = TRUE
        ) INTO v_is_subscribed;

        RETURN v_is_subscribed;
    END IF;

    RETURN FALSE;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION user_can_access_content IS 'Check if user can access content based on subscription or purchase status';

-- ============================================================================
-- DONE - Migration complete
-- ============================================================================
