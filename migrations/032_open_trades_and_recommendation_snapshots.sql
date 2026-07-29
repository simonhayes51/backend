-- Run this migration against DATABASE_URL (the database containing trades) only.
BEGIN;

ALTER TABLE trades ADD COLUMN IF NOT EXISTS card_id BIGINT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS bought_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS sold_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'closed';
ALTER TABLE trades ADD COLUMN IF NOT EXISTS target_sell INTEGER;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_status TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_strategy TEXT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_confidence NUMERIC;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_expected_roi NUMERIC;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_buy_below INTEGER;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_sell_around INTEGER;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_fair_value INTEGER;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS recommendation_snapshot JSONB;

ALTER TABLE trades ALTER COLUMN sell DROP NOT NULL;

-- Legacy deployments stored trades.timestamp as TEXT. Cast it explicitly and
-- fall back to NOW() only for blank/invalid historical values.
UPDATE trades
SET bought_at = COALESCE(
        bought_at,
        CASE
            WHEN NULLIF(BTRIM(timestamp::text), '') IS NOT NULL
             AND pg_input_is_valid(timestamp::text, 'timestamp with time zone')
            THEN timestamp::timestamptz
            ELSE NOW()
        END
    ),
    sold_at = CASE
        WHEN sell IS NOT NULL THEN COALESCE(
            sold_at,
            CASE
                WHEN NULLIF(BTRIM(timestamp::text), '') IS NOT NULL
                 AND pg_input_is_valid(timestamp::text, 'timestamp with time zone')
                THEN timestamp::timestamptz
                ELSE NOW()
            END
        )
        ELSE sold_at
    END,
    status = CASE WHEN sell IS NULL THEN 'open' ELSE 'closed' END
WHERE bought_at IS NULL OR status IS NULL OR (sell IS NOT NULL AND sold_at IS NULL);

ALTER TABLE trades DROP CONSTRAINT IF EXISTS trades_status_check;
ALTER TABLE trades ADD CONSTRAINT trades_status_check CHECK (status IN ('open','closed'));

CREATE INDEX IF NOT EXISTS idx_trades_user_status ON trades(user_id, status, bought_at DESC);
CREATE INDEX IF NOT EXISTS idx_trades_user_sold_at ON trades(user_id, sold_at DESC) WHERE status='closed';
CREATE INDEX IF NOT EXISTS idx_trades_card_id ON trades(card_id) WHERE card_id IS NOT NULL;

COMMIT;
