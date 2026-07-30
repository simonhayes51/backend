-- Migration 034: expose generated-card columns through fair_value_mv
-- target: player
-- requires-table: fut_players, sales_history, bin_history
--
-- Same DROP + CREATE rebuild pattern as migration 023 (Postgres has no
-- ALTER MATERIALIZED VIEW for the underlying query) - unchanged except for
-- passing through fut_players' generated_card_url/generated_card_status/
-- generated_card_at/generated_card_flagged (migrations 025/026), so
-- /api/dashboard/stats' Movers section, /api/market/fair-value/*, and
-- /api/v2/market/fair-value/batch can trigger on-demand generation
-- (app/services/player_card_ondemand.py) for cards they surface without an
-- extra per-card fut_players lookup of their own.

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
    p.card_bg_image,
    p.card_cutout_image,
    p.card_cutout_type,
    p.card_name,
    p.generated_card_url,
    p.generated_card_status,
    p.generated_card_at,
    p.generated_card_flagged,
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
