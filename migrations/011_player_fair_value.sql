-- Migration 011: Fair Value engine — run against the PLAYER database
-- (PLAYER_DATABASE_URL; same as DATABASE_URL on default single-DB deploys).
-- target: player
-- requires-table: sales_history, bin_history
--
-- sales_history/bin_history are created by the auto_sync service's own
-- ensure_tables() on its first run, not by any migration in this repo. If
-- the backend boots and runs migrations before auto_sync has ever run
-- against this database, this migration depends on tables that don't
-- exist yet - the requires-table line above makes the runner skip it
-- cleanly and retry next boot instead of surfacing a raw Postgres
-- "relation does not exist" error.
--
-- Builds the precomputed fair-value layer over sales_history + bin_history:
--   fair_value_mv: one row per card with real median sold prices,
--   liquidity, volatility, current BIN, and discount vs fair value.
--
-- Refreshed CONCURRENTLY every FAIR_VALUE_REFRESH_SECONDS (default 300)
-- by the backend's fair-value refresher task (app/services/fair_value.py),
-- guarded by a pg advisory lock so multiple instances don't stampede.

-- Hot-path indexes (sales_history grows forever; these keep the MV refresh
-- and the raw history endpoints fast).
CREATE INDEX IF NOT EXISTS idx_sales_history_player_time
    ON sales_history (player_id, sold_at DESC);
CREATE INDEX IF NOT EXISTS idx_bin_history_player_time
    ON bin_history (player_id, captured_at DESC);

CREATE MATERIALIZED VIEW IF NOT EXISTS fair_value_mv AS
WITH sales AS (
    SELECT
        player_id,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE sold_at >= now() - interval '24 hours')  AS fair_value_24h,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE sold_at >= now() - interval '7 days')    AS fair_value_7d,
        count(*) FILTER (WHERE sold_at >= now() - interval '24 hours') AS sales_24h,
        count(*) FILTER (WHERE sold_at >= now() - interval '7 days')   AS sales_7d,
        stddev_pop(sold_price)
            FILTER (WHERE sold_at >= now() - interval '24 hours')  AS volatility_24h,
        max(sold_at)                                               AS last_sale_at
    FROM sales_history
    WHERE sold_at >= now() - interval '7 days'
    GROUP BY player_id
),
latest_bin AS (
    SELECT DISTINCT ON (player_id, platform)
        player_id, platform, lowest_bin, captured_at
    FROM bin_history
    WHERE captured_at >= now() - interval '48 hours'
    ORDER BY player_id, platform, captured_at DESC
),
ps_bin AS (
    SELECT player_id, lowest_bin AS current_bin, captured_at AS bin_captured_at
    FROM latest_bin WHERE platform = 'ps'
)
SELECT
    s.player_id                                   AS card_id,
    p.name,
    p.rating,
    p.version,
    p.position,
    p.image_url,
    round(s.fair_value_24h)::bigint               AS fair_value_24h,
    round(s.fair_value_7d)::bigint                AS fair_value_7d,
    s.sales_24h,
    s.sales_7d,
    round(s.sales_24h / 24.0, 2)                  AS sales_per_hour_24h,
    round(s.volatility_24h)::bigint               AS volatility_24h,
    s.last_sale_at,
    b.current_bin,
    b.bin_captured_at,
    CASE
        WHEN b.current_bin IS NOT NULL AND s.fair_value_24h > 0
        THEN round(((s.fair_value_24h - b.current_bin) / s.fair_value_24h * 100)::numeric, 2)
        ELSE NULL
    END                                           AS discount_pct,
    -- z-score of current BIN against the 24h sold distribution: strongly
    -- negative = priced well below where it's really clearing = opportunity
    CASE
        WHEN b.current_bin IS NOT NULL AND coalesce(s.volatility_24h, 0) > 0
        THEN round(((b.current_bin - s.fair_value_24h) / s.volatility_24h)::numeric, 2)
        ELSE NULL
    END                                           AS bin_zscore_24h,
    now()                                         AS computed_at
FROM sales s
LEFT JOIN ps_bin b USING (player_id)
LEFT JOIN fut_players p ON p.card_id = s.player_id;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS idx_fair_value_mv_card ON fair_value_mv (card_id);
CREATE INDEX IF NOT EXISTS idx_fair_value_mv_discount
    ON fair_value_mv (discount_pct DESC NULLS LAST);
