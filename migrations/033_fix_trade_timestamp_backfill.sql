-- Run this migration against DATABASE_URL (the database containing trades) only.
-- Repairs the legacy trades.timestamp TEXT -> TIMESTAMPTZ backfill used by migration 032.
BEGIN;

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
WHERE bought_at IS NULL
   OR status IS NULL
   OR (sell IS NOT NULL AND sold_at IS NULL);

COMMIT;
