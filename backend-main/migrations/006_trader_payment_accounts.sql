-- Migration: Trader Payment Accounts (Stripe Connect & PayPal Marketplace)
-- Date: 2026-01-16
-- Description: Enable traders to connect their own payment accounts to receive payments directly

-- ============================================================================
-- 1. Add payment account columns to trader_profiles
-- ============================================================================

DO $$
BEGIN
    -- Stripe Connect account ID
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'stripe_connect_account_id'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN stripe_connect_account_id VARCHAR(255);
    END IF;

    -- Stripe Connect onboarding status
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'stripe_connect_status'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN stripe_connect_status VARCHAR(50) DEFAULT 'not_started'
        CHECK (stripe_connect_status IN ('not_started', 'pending', 'active', 'rejected', 'disabled'));
    END IF;

    -- Stripe Connect onboarding completion
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'stripe_charges_enabled'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN stripe_charges_enabled BOOLEAN DEFAULT FALSE;
    END IF;

    -- Stripe Connect payouts enabled
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'stripe_payouts_enabled'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN stripe_payouts_enabled BOOLEAN DEFAULT FALSE;
    END IF;

    -- PayPal Merchant ID (for Commerce Platform)
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'paypal_merchant_id'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN paypal_merchant_id VARCHAR(255);
    END IF;

    -- PayPal merchant status
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'paypal_merchant_status'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN paypal_merchant_status VARCHAR(50) DEFAULT 'not_started'
        CHECK (paypal_merchant_status IN ('not_started', 'pending', 'active', 'restricted', 'disabled'));
    END IF;

    -- PayPal email for receiving payments
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'paypal_email'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN paypal_email VARCHAR(255);
    END IF;

    -- Payment account setup completion tracking
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'payment_setup_completed'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN payment_setup_completed BOOLEAN DEFAULT FALSE;
    END IF;

    -- Last payment account update
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'trader_profiles'
        AND column_name = 'payment_accounts_updated_at'
    ) THEN
        ALTER TABLE trader_profiles
        ADD COLUMN payment_accounts_updated_at TIMESTAMP;
    END IF;
END $$;

-- Indexes for payment account lookups
CREATE INDEX IF NOT EXISTS idx_trader_profiles_stripe_connect
ON trader_profiles(stripe_connect_account_id)
WHERE stripe_connect_account_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trader_profiles_paypal_merchant
ON trader_profiles(paypal_merchant_id)
WHERE paypal_merchant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trader_profiles_payment_setup
ON trader_profiles(payment_setup_completed);

-- Comments
COMMENT ON COLUMN trader_profiles.stripe_connect_account_id IS 'Stripe Connect account ID for receiving payments';
COMMENT ON COLUMN trader_profiles.stripe_connect_status IS 'Stripe Connect onboarding status';
COMMENT ON COLUMN trader_profiles.stripe_charges_enabled IS 'Whether Stripe account can accept charges';
COMMENT ON COLUMN trader_profiles.stripe_payouts_enabled IS 'Whether Stripe account can receive payouts';
COMMENT ON COLUMN trader_profiles.paypal_merchant_id IS 'PayPal merchant/partner ID for receiving payments';
COMMENT ON COLUMN trader_profiles.paypal_merchant_status IS 'PayPal merchant account status';
COMMENT ON COLUMN trader_profiles.paypal_email IS 'PayPal email address for receiving payments';
COMMENT ON COLUMN trader_profiles.payment_setup_completed IS 'TRUE if trader has set up at least one payment method';

-- ============================================================================
-- 2. Create platform_fees table for fee configuration
-- ============================================================================

CREATE TABLE IF NOT EXISTS platform_fees (
    id SERIAL PRIMARY KEY,
    fee_type VARCHAR(50) NOT NULL CHECK (fee_type IN ('subscription', 'content_purchase', 'tip')),
    fee_percentage DECIMAL(5,2) NOT NULL DEFAULT 10.00 CHECK (fee_percentage >= 0 AND fee_percentage <= 100),
    fee_fixed_amount DECIMAL(10,2) DEFAULT 0.00,
    currency VARCHAR(3) DEFAULT 'GBP',
    active BOOLEAN DEFAULT TRUE,
    effective_from TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    effective_until TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT unique_active_fee_type UNIQUE (fee_type, active)
);

-- Default platform fees (10% for everything)
INSERT INTO platform_fees (fee_type, fee_percentage, fee_fixed_amount)
VALUES
    ('subscription', 10.00, 0.00),
    ('content_purchase', 10.00, 0.00),
    ('tip', 5.00, 0.00)
ON CONFLICT DO NOTHING;

COMMENT ON TABLE platform_fees IS 'Platform fee configuration for different transaction types';
COMMENT ON COLUMN platform_fees.fee_percentage IS 'Percentage fee (e.g., 10.00 for 10%)';
COMMENT ON COLUMN platform_fees.fee_fixed_amount IS 'Fixed fee amount in addition to percentage';

-- ============================================================================
-- 3. Create payouts table for tracking trader earnings transfers
-- ============================================================================

CREATE TABLE IF NOT EXISTS trader_payouts (
    id BIGSERIAL PRIMARY KEY,
    trader_id BIGINT NOT NULL,

    -- Payout details
    amount DECIMAL(10,2) NOT NULL,
    currency VARCHAR(3) DEFAULT 'GBP',

    -- Payment provider
    provider VARCHAR(50) NOT NULL CHECK (provider IN ('stripe', 'paypal', 'manual')),

    -- Provider-specific IDs
    stripe_payout_id VARCHAR(255),
    stripe_transfer_id VARCHAR(255),
    paypal_payout_batch_id VARCHAR(255),
    paypal_payout_item_id VARCHAR(255),

    -- Status tracking
    status VARCHAR(50) DEFAULT 'pending' CHECK (status IN (
        'pending', 'in_transit', 'paid', 'failed', 'canceled', 'reversed'
    )),

    -- Period covered
    period_start TIMESTAMP NOT NULL,
    period_end TIMESTAMP NOT NULL,

    -- Timestamps
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    paid_at TIMESTAMP,
    failed_at TIMESTAMP,

    -- Metadata
    description TEXT,
    failure_reason TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,

    FOREIGN KEY (trader_id) REFERENCES users(id) ON DELETE CASCADE
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_trader_payouts_trader ON trader_payouts(trader_id);
CREATE INDEX IF NOT EXISTS idx_trader_payouts_status ON trader_payouts(status);
CREATE INDEX IF NOT EXISTS idx_trader_payouts_stripe ON trader_payouts(stripe_payout_id) WHERE stripe_payout_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trader_payouts_paypal ON trader_payouts(paypal_payout_batch_id) WHERE paypal_payout_batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_trader_payouts_period ON trader_payouts(period_start, period_end);

COMMENT ON TABLE trader_payouts IS 'Tracks payouts to traders for their earnings';

-- ============================================================================
-- 4. Update trader_earnings view to include platform fees
-- ============================================================================

DROP VIEW IF EXISTS trader_earnings;

CREATE OR REPLACE VIEW trader_earnings AS
SELECT
    tp.user_id as trader_id,
    up.username,

    -- Payment account status
    tp.stripe_connect_account_id,
    tp.stripe_connect_status,
    tp.stripe_charges_enabled,
    tp.paypal_merchant_id,
    tp.paypal_merchant_status,
    tp.payment_setup_completed,

    -- Subscription earnings (monthly recurring)
    COUNT(DISTINCT ts.id) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')) as active_subscribers,
    COALESCE(SUM(ts.amount) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) as gross_monthly_subscription_revenue,

    -- Platform fees on subscriptions (10%)
    COALESCE(SUM(ts.amount * 0.10) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) as subscription_platform_fees,

    -- Net subscription revenue
    COALESCE(SUM(ts.amount * 0.90) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) as net_monthly_subscription_revenue,

    -- One-off content sales (lifetime)
    COUNT(DISTINCT cp.id) FILTER (WHERE cp.status = 'completed') as content_sales,
    COALESCE(SUM(cp.amount) FILTER (WHERE cp.status = 'completed'), 0) as gross_content_revenue,

    -- Platform fees on content (10%)
    COALESCE(SUM(cp.amount * 0.10) FILTER (WHERE cp.status = 'completed'), 0) as content_platform_fees,

    -- Net content revenue
    COALESCE(SUM(cp.amount * 0.90) FILTER (WHERE cp.status = 'completed'), 0) as net_content_revenue,

    -- Tips (lifetime) - 5% platform fee
    COALESCE(SUM(pt.amount), 0) as gross_tips_received,
    COALESCE(SUM(pt.amount * 0.05), 0) as tips_platform_fees,
    COALESCE(SUM(pt.amount * 0.95), 0) as net_tips_received,

    -- Combined metrics
    COALESCE(SUM(ts.amount * 0.90) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) as estimated_net_monthly_revenue,

    -- Total platform fees
    COALESCE(SUM(ts.amount * 0.10) FILTER (WHERE ts.is_active = TRUE AND ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) +
    COALESCE(SUM(cp.amount * 0.10) FILTER (WHERE cp.status = 'completed'), 0) +
    COALESCE(SUM(pt.amount * 0.05), 0) as total_platform_fees,

    -- Lifetime earnings available for payout
    COALESCE(SUM(ts.amount * 0.90) FILTER (WHERE ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) +
    COALESCE(SUM(cp.amount * 0.90) FILTER (WHERE cp.status = 'completed'), 0) +
    COALESCE(SUM(pt.amount * 0.95), 0) as lifetime_earnings_before_payouts,

    -- Already paid out
    COALESCE(SUM(po.amount) FILTER (WHERE po.status = 'paid'), 0) as total_paid_out,

    -- Available balance
    COALESCE(SUM(ts.amount * 0.90) FILTER (WHERE ts.subscription_type IN ('paid', 'basic', 'premium', 'elite')), 0) +
    COALESCE(SUM(cp.amount * 0.90) FILTER (WHERE cp.status = 'completed'), 0) +
    COALESCE(SUM(pt.amount * 0.95), 0) -
    COALESCE(SUM(po.amount) FILTER (WHERE po.status = 'paid'), 0) as available_balance

FROM trader_profiles tp
LEFT JOIN user_profiles up ON tp.user_id = up.user_id
LEFT JOIN trader_subscriptions ts ON tp.user_id = ts.trader_id
LEFT JOIN social_posts sp ON tp.user_id = sp.user_id
LEFT JOIN content_purchases cp ON sp.id = cp.post_id
LEFT JOIN post_tips pt ON sp.id = pt.post_id
LEFT JOIN trader_payouts po ON tp.user_id::text = po.trader_id::text
GROUP BY tp.user_id, up.username, tp.stripe_connect_account_id, tp.stripe_connect_status,
         tp.stripe_charges_enabled, tp.paypal_merchant_id, tp.paypal_merchant_status, tp.payment_setup_completed;

COMMENT ON VIEW trader_earnings IS 'Analytics view for trader revenue including platform fees and available balance';

-- ============================================================================
-- 5. Function to calculate platform fee
-- ============================================================================

CREATE OR REPLACE FUNCTION calculate_platform_fee(
    p_fee_type VARCHAR(50),
    p_amount DECIMAL(10,2)
) RETURNS DECIMAL(10,2) AS $$
DECLARE
    v_fee_percentage DECIMAL(5,2);
    v_fee_fixed DECIMAL(10,2);
    v_total_fee DECIMAL(10,2);
BEGIN
    -- Get active fee configuration
    SELECT fee_percentage, fee_fixed_amount
    INTO v_fee_percentage, v_fee_fixed
    FROM platform_fees
    WHERE fee_type = p_fee_type
    AND active = TRUE
    AND (effective_until IS NULL OR effective_until > CURRENT_TIMESTAMP)
    ORDER BY effective_from DESC
    LIMIT 1;

    -- Default to 10% if not found
    IF v_fee_percentage IS NULL THEN
        v_fee_percentage := 10.00;
        v_fee_fixed := 0.00;
    END IF;

    -- Calculate fee
    v_total_fee := (p_amount * v_fee_percentage / 100) + v_fee_fixed;

    RETURN ROUND(v_total_fee, 2);
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION calculate_platform_fee IS 'Calculate platform fee for a transaction based on type and amount';

-- ============================================================================
-- 6. Function to check if trader can receive payments
-- ============================================================================

CREATE OR REPLACE FUNCTION trader_can_receive_payments(
    p_trader_id TEXT,
    p_provider VARCHAR(50) DEFAULT NULL
) RETURNS BOOLEAN AS $$
DECLARE
    v_trader RECORD;
BEGIN
    -- Get trader payment account info
    SELECT
        stripe_connect_account_id,
        stripe_charges_enabled,
        paypal_merchant_id,
        paypal_merchant_status,
        payment_setup_completed
    INTO v_trader
    FROM trader_profiles
    WHERE user_id = p_trader_id;

    IF NOT FOUND THEN
        RETURN FALSE;
    END IF;

    -- If provider specified, check that specific provider
    IF p_provider = 'stripe' THEN
        RETURN v_trader.stripe_connect_account_id IS NOT NULL
               AND v_trader.stripe_charges_enabled = TRUE;
    ELSIF p_provider = 'paypal' THEN
        RETURN v_trader.paypal_merchant_id IS NOT NULL
               AND v_trader.paypal_merchant_status = 'active';
    ELSE
        -- Check if at least one payment method is set up
        RETURN v_trader.payment_setup_completed = TRUE;
    END IF;
END;
$$ LANGUAGE plpgsql;

COMMENT ON FUNCTION trader_can_receive_payments IS 'Check if trader has payment accounts set up to receive payments';

-- ============================================================================
-- DONE - Migration complete
-- ============================================================================
