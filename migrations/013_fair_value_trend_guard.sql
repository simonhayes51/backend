-- Migration 013: trend_falling guard on fair_value_mv
-- target: player
-- requires-table: sales_history, bin_history
--
-- Editing 011/012 in place has no effect once they've run (Postgres has no
-- ALTER MATERIALIZED VIEW for the underlying query, only DROP + CREATE) -
-- this rebuilds fair_value_mv again, adding a trend_falling flag.
--
-- Context: discount_pct compares a live, instantaneous current_bin against
-- fair_value_24h, a TRAILING median of the last 24h of sales. Those have
-- very different time horizons. When a card is mid-crash (something makes
-- it permanently worth less - a meta shift, a cheaper alternative, an SBC
-- reward flooding supply - and it never recovers), current_bin reflects
-- the new, lower reality within minutes, while fair_value_24h stays
-- anchored near yesterday's higher price for most of a day, since it's
-- built from a blend of pre- and post-crash sales. The result: discount_pct
-- comes out large and positive - a confident "STEAL" - at exactly the
-- moment the price is falling and won't bounce back. The verdict has no
-- concept of direction, only "how far below the trailing average", so it
-- can't tell "temporarily cheap, about to revert" from "resetting lower
-- for good" until a full 24h window has rolled past the crash.
--
-- trend_falling adds a faster, more recent signal on both sides:
--   - bin_recent_ref: the median PS BIN from 2-6h ago (a short lookback,
--     not the single latest snapshot, so one noisy listing can't trip it)
--   - fair_value_2h/sales_2h: median sold price in just the last 2 hours
-- and flags true if either shows the market has already moved meaningfully
-- below where the 24h median still sits. Consumers (undervalued board,
-- anomaly radar, the verdict shown to users) treat trend_falling as an
-- override: never call a falling card a "steal", regardless of discount_pct.

DROP MATERIALIZED VIEW IF EXISTS fair_value_mv;

CREATE MATERIALIZED VIEW fair_value_mv AS
WITH sales AS (
    SELECT
        player_id,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE sold_at >= now() - interval '24 hours')  AS fair_value_24h,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE sold_at >= now() - interval '7 days')    AS fair_value_7d,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)
            FILTER (WHERE sold_at >= now() - interval '2 hours')   AS fair_value_2h,
        count(*) FILTER (WHERE sold_at >= now() - interval '24 hours') AS sales_24h,
        count(*) FILTER (WHERE sold_at >= now() - interval '7 days')   AS sales_7d,
        count(*) FILTER (WHERE sold_at >= now() - interval '2 hours')  AS sales_2h,
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
),
recent_bin AS (
    -- Where the PS BIN sat a few hours ago, not right now - a short-lookback
    -- reference point for detecting a live price drop before the 24h sold
    -- median has had time to catch up.
    SELECT player_id,
           percentile_cont(0.5) WITHIN GROUP (ORDER BY lowest_bin) AS bin_recent_ref
    FROM bin_history
    WHERE platform = 'ps'
      AND captured_at >= now() - interval '6 hours'
      AND captured_at <  now() - interval '2 hours'
    GROUP BY player_id
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
    CASE
        WHEN b.current_bin IS NOT NULL AND coalesce(s.volatility_24h, 0) > 0
        THEN round(((b.current_bin - s.fair_value_24h) / s.volatility_24h)::numeric, 2)
        ELSE NULL
    END                                           AS bin_zscore_24h,
    CASE
        WHEN b.current_bin IS NOT NULL AND s.fair_value_24h > 0
             AND (s.fair_value_24h < b.current_bin * 0.1 OR s.fair_value_24h > b.current_bin * 10)
        THEN true
        ELSE false
    END                                           AS data_quality_suspect,
    CASE
        WHEN r.bin_recent_ref IS NOT NULL AND r.bin_recent_ref > 0
             AND b.current_bin IS NOT NULL
             AND b.current_bin <= r.bin_recent_ref * 0.85
        THEN true
        WHEN s.fair_value_2h IS NOT NULL AND coalesce(s.sales_2h, 0) >= 3
             AND s.fair_value_24h > 0
             AND s.fair_value_2h <= s.fair_value_24h * 0.85
        THEN true
        ELSE false
    END                                           AS trend_falling,
    now()                                         AS computed_at
FROM sales s
LEFT JOIN ps_bin b USING (player_id)
LEFT JOIN recent_bin r USING (player_id)
LEFT JOIN fut_players p ON p.card_id = s.player_id;

CREATE UNIQUE INDEX idx_fair_value_mv_card ON fair_value_mv (card_id);
CREATE INDEX idx_fair_value_mv_discount ON fair_value_mv (discount_pct DESC NULLS LAST);
CREATE INDEX idx_fair_value_mv_suspect ON fair_value_mv (data_quality_suspect) WHERE data_quality_suspect;
CREATE INDEX idx_fair_value_mv_falling ON fair_value_mv (trend_falling) WHERE trend_falling;
