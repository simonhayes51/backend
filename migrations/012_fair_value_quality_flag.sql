-- Migration 012: add data_quality_suspect to fair_value_mv
-- target: player
-- requires-table: sales_history, bin_history
--
-- Editing 011 in place has no effect on a database where it already ran
-- (it's recorded in schema_migrations and never re-executes) - and
-- Postgres has no ALTER MATERIALIZED VIEW for changing the underlying
-- query, only DROP + CREATE. This migration rebuilds fair_value_mv with
-- one addition: a data_quality_suspect flag.
--
-- Context: a resolved incident showed sales_history can end up with
-- real, internally-consistent sales data (correct EA tax math) recorded
-- for the WRONG card entirely - a scraper URL-resolution bug
-- (_sales_url's "/player/ -> /sales/" fallback, now removed) guessed a
-- sales page instead of confirming one, and for brand-new cards without
-- a confirmed link yet, that guess sometimes landed on a different,
-- unrelated card's real sales. That specific bug is fixed, but this flag
-- guards against ANY future/unknown cause of the same symptom: if our
-- own median is wildly inconsistent with the live BIN (more than 10x
-- either direction - a ratio, not a fixed gap, since normal price bands
-- differ hugely across ratings), that's not a genuine "great discount",
-- it's a sign this card's data can't be trusted yet. Consumers
-- (app/services/fair_value.py) exclude suspect rows from the
-- undervalued/anomaly boards rather than presenting contaminated data as
-- a confident "steal".

DROP MATERIALIZED VIEW IF EXISTS fair_value_mv;

CREATE MATERIALIZED VIEW fair_value_mv AS
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
    now()                                         AS computed_at
FROM sales s
LEFT JOIN ps_bin b USING (player_id)
LEFT JOIN fut_players p ON p.card_id = s.player_id;

CREATE UNIQUE INDEX idx_fair_value_mv_card ON fair_value_mv (card_id);
CREATE INDEX idx_fair_value_mv_discount ON fair_value_mv (discount_pct DESC NULLS LAST);
CREATE INDEX idx_fair_value_mv_suspect ON fair_value_mv (data_quality_suspect) WHERE data_quality_suspect;
