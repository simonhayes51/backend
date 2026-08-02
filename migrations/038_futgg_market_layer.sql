-- Migration 038: FUT.GG market-intelligence layer
-- target: core
-- requires-table: futgg_players, futgg_bin_history, futgg_sales_history
--
-- Purely additive: creates a provider-neutral, FUT.GG-backed market layer
-- alongside (not instead of) the existing FUTBIN-backed fair_value_mv /
-- fut_players / sales_history / bin_history stack. Nothing here alters,
-- drops, or reads from any FUTBIN table, and futgg_players/futgg_bin_
-- history/futgg_sales_history themselves are NOT owned or altered by this
-- migration - they're created and populated entirely by the auto_sync
-- repo's scraper; this migration only ever reads them (hence
-- "requires-table" above, so the runner skips cleanly on a database that
-- hasn't run auto_sync's own bootstrap yet, and retries next boot -
-- same pattern migration 011 established for sales_history/bin_history).
--
-- Two objects:
--
--   1. card_source_map - an EMPTY identity-mapping table for a future
--      FUTGG<->FUTBIN reconciliation pass (fuzzy name/rating/position
--      matching, or manual review). Deliberately not populated here -
--      that matching logic is its own piece of future work and out of
--      scope for this migration; the table just needs to exist so that
--      work has somewhere to write results without a further migration.
--
--   2. futgg_market_snapshot - a MATERIALIZED view (not a plain view).
--      A plain view was considered first (it would always be exactly as
--      fresh as the underlying tables, and needs no refresh scheduling),
--      but was rejected: this view's per-card aggregates come from
--      GROUP BY/percentile_cont over futgg_sales_history, and Postgres
--      cannot push an outer "WHERE rating >= 85 LIMIT 50"-style predicate
--      from /api/v2/players or /api/v2/opportunities down through that
--      aggregation - so every list-style request would re-aggregate the
--      ENTIRE sales history table before filtering, which is exactly the
--      "no full-table scans of futgg_sales_history per request" hazard
--      the task calls out. A materialized view pre-computes the
--      aggregation once and is then just an indexed table scan/lookup
--      per request, same as fair_value_mv already does for the legacy
--      FUTBIN path (see migrations/023 and 034's own comments on why
--      that one is materialized too).
--
--      REFRESH CONVENTION - mirrors fair_value_mv/recommendations_latest
--      (see app/services/fair_value.py::refresher_loop and
--      app/services/recommendation_engine_v2.py::run_pass_v2): call
--
--          REFRESH MATERIALIZED VIEW CONCURRENTLY futgg_market_snapshot;
--
--      periodically from app/services/market_data_provider.py's own
--      refresher_loop (wired into main.py's lifespan next to the other
--      refreshers), guarded by pg_try_advisory_lock so multiple app
--      instances never race the refresh. CONCURRENTLY requires a unique
--      index on the view, created below, and requires the view to
--      already have been populated once (hence "WITH DATA" on CREATE).
--
--      Recent-sales window: bounded to each card's most recent 50 sales
--      rows (source_row_position-independent - ordered by
--      approximate_sold_at) captured within the trailing 14 days,
--      whichever is smaller. 50 rows matches futgg_sales_history's own
--      typical per-card page size (see table comment in the task spec:
--      "up to ~50 rows per card"), and 14 days bounds the window for
--      cards that sell slowly enough that 50 rows would otherwise reach
--      back further than is still representative of the *current*
--      market. Both bounds are deliberately generous starting points,
--      not tuned thresholds - revisit once real query patterns exist.

CREATE TABLE IF NOT EXISTS card_source_map (
    internal_card_key BIGSERIAL PRIMARY KEY,
    futgg_source_card_id BIGINT UNIQUE,
    futbin_card_id BIGINT,
    legacy_card_id BIGINT,
    match_method TEXT,
    match_confidence NUMERIC(5,4),
    reviewed BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_card_source_map_futbin ON card_source_map (futbin_card_id);
CREATE INDEX IF NOT EXISTS idx_card_source_map_legacy ON card_source_map (legacy_card_id);

-- Postgres has no CREATE OR REPLACE MATERIALIZED VIEW, only DROP + CREATE
-- (same reason migrations 023/034 rebuild fair_value_mv wholesale rather
-- than ALTER it). Safe to re-run: DROP ... IF EXISTS, then CREATE fresh.
DROP MATERIALIZED VIEW IF EXISTS futgg_market_snapshot;

CREATE MATERIALIZED VIEW futgg_market_snapshot AS
WITH latest_bin AS (
    SELECT DISTINCT ON (source_card_id)
        source_card_id,
        lowest_bin            AS current_bin,
        price_range_low,
        price_range_high,
        source_age_text       AS bin_source_age_text,
        captured_at           AS bin_captured_at
    FROM futgg_bin_history
    ORDER BY source_card_id, captured_at DESC
),
bounded_sales AS (
    SELECT source_card_id, sold_price, approximate_sold_at
    FROM (
        SELECT
            source_card_id, sold_price, approximate_sold_at,
            ROW_NUMBER() OVER (
                PARTITION BY source_card_id ORDER BY approximate_sold_at DESC
            ) AS rn
        FROM futgg_sales_history
        WHERE approximate_sold_at >= now() - interval '14 days'
    ) ranked
    WHERE rn <= 50
),
sales_stats AS (
    SELECT
        source_card_id,
        count(*)                                                       AS sales_count,
        percentile_cont(0.5) WITHIN GROUP (ORDER BY sold_price)         AS sales_median,
        percentile_cont(0.1) WITHIN GROUP (ORDER BY sold_price)         AS sales_p10,
        percentile_cont(0.9) WITHIN GROUP (ORDER BY sold_price)         AS sales_p90,
        min(sold_price)                                                AS sales_low,
        max(sold_price)                                                AS sales_high,
        stddev_pop(sold_price)                                         AS sales_stddev,
        min(approximate_sold_at)                                       AS sales_window_earliest_at,
        max(approximate_sold_at)                                       AS sales_window_latest_at
    FROM bounded_sales
    GROUP BY source_card_id
),
-- Trimmed mean: average of only the sales strictly between the group's
-- own p10/p90 (a second pass over bounded_sales joined back to
-- sales_stats - Postgres has no built-in trimmed-mean aggregate).
-- Falls back to the plain mean below when a card's sample is too thin
-- for p10 != p90 to exclude anything (LEFT JOIN leaves sales_trimmed_mean
-- NULL for those, resolved with COALESCE in the final SELECT).
trimmed AS (
    SELECT
        bs.source_card_id,
        avg(bs.sold_price) AS sales_trimmed_mean
    FROM bounded_sales bs
    JOIN sales_stats ss ON ss.source_card_id = bs.source_card_id
    WHERE bs.sold_price BETWEEN ss.sales_p10 AND ss.sales_p90
    GROUP BY bs.source_card_id
),
latest_sale AS (
    SELECT DISTINCT ON (source_card_id)
        source_card_id,
        sold_price       AS latest_sale_price,
        approximate_sold_at AS latest_sale_at
    FROM bounded_sales
    ORDER BY source_card_id, approximate_sold_at DESC
)
SELECT
    p.source_card_id,
    p.source_player_id,
    p.source_slug,
    p.source_url,
    p.game_year,
    p.name,
    p.rating,
    p.primary_position,
    p.alternate_positions,
    p.rarity,
    p.squad,
    p.price_tier,
    p.club,
    p.league,
    p.nation,
    p.club_source_id,
    p.league_source_id,
    p.nation_source_id,
    p.player_image_url,
    p.card_design_image_url,
    p.club_image_url,
    p.league_image_url,
    p.nation_image_url,
    p.is_active,
    p.is_tradeable,
    p.last_price_status,
    p.metadata_warnings,
    p.price_updated_at,
    p.next_price_due_at,

    lb.current_bin,
    lb.price_range_low,
    lb.price_range_high,
    lb.bin_source_age_text,
    lb.bin_captured_at,

    coalesce(ss.sales_count, 0)                          AS sales_count,
    ss.sales_median,
    coalesce(t.sales_trimmed_mean, ss.sales_median)       AS sales_trimmed_mean,
    ss.sales_low,
    ss.sales_high,
    ss.sales_stddev,
    ls.latest_sale_price,
    ls.latest_sale_at,
    ss.sales_window_earliest_at,
    ss.sales_window_latest_at,
    CASE
        WHEN ss.sales_window_earliest_at IS NOT NULL AND ss.sales_window_latest_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (ss.sales_window_latest_at - ss.sales_window_earliest_at)) / 60.0
        ELSE NULL
    END                                                   AS sales_window_span_minutes,
    CASE
        WHEN ss.sales_median IS NOT NULL AND ss.sales_median > 0 AND ss.sales_stddev IS NOT NULL
        THEN round((ss.sales_stddev / ss.sales_median)::numeric, 4)
        ELSE NULL
    END                                                   AS sales_dispersion_ratio,

    now()                                                 AS snapshot_computed_at
FROM futgg_players p
LEFT JOIN latest_bin lb  ON lb.source_card_id = p.source_card_id
LEFT JOIN sales_stats ss ON ss.source_card_id = p.source_card_id
LEFT JOIN trimmed t      ON t.source_card_id  = p.source_card_id
LEFT JOIN latest_sale ls ON ls.source_card_id = p.source_card_id
WHERE p.is_active
WITH DATA;

-- Required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS idx_futgg_market_snapshot_card
    ON futgg_market_snapshot (source_card_id);

CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_rating
    ON futgg_market_snapshot (rating);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_position
    ON futgg_market_snapshot (primary_position);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_rarity
    ON futgg_market_snapshot (rarity);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_club
    ON futgg_market_snapshot (club);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_league
    ON futgg_market_snapshot (league);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_nation
    ON futgg_market_snapshot (nation);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_current_bin
    ON futgg_market_snapshot (current_bin);
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_tradeable
    ON futgg_market_snapshot (is_tradeable) WHERE is_tradeable IS NOT FALSE;
-- Plain btree on lower(name) rather than a pg_trgm GIN index: pg_trgm is
-- a contrib extension that may not be installable without superuser on
-- every managed Postgres this runs against, and this migration must stay
-- runnable without assuming extension-install privileges. This still
-- gives ILIKE 'prefix%' (case-insensitive) an index to use; genuinely
-- diacritic-insensitive substring search is done application-side (see
-- app/services/market_data_provider.py's search normalization) over the
-- ILIKE-narrowed candidate set, not by this index.
CREATE INDEX IF NOT EXISTS idx_futgg_market_snapshot_name_lower
    ON futgg_market_snapshot (lower(name));
