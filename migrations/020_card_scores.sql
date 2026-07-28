-- Migration 020: Card Scores (Analytics Engine's fixed contract)
-- target: player
-- requires-table: fut_players
--
-- Deliberately does NOT list fair_value_mv here - materialized views
-- never appear in information_schema.tables (only pg_matviews/
-- pg_class.relkind='m' show them), confirmed against
-- app/services/fair_value.py's own ensure_fair_value_mv, which checks
-- pg_matviews directly rather than this runner's _missing_tables()
-- helper. card_scores' only real FK dependency is fut_players.
--
-- Append-only, partitioned by month so a score finally has real
-- history (the exact problem migration 013 had to work around for
-- fair_value_mv's trend_falling with no history table at all). Only
-- the DEFAULT partition is created here - real dated monthly
-- partitions are created idempotently at runtime by
-- analytics_engine.ensure_card_scores_partitions() (mirrors
-- ensure_fair_value_mv's create-if-missing idiom), so this migration
-- never goes stale with hardcoded dates regardless of when it deploys.

CREATE TABLE IF NOT EXISTS card_scores (
    id             BIGSERIAL,
    card_id        BIGINT NOT NULL REFERENCES fut_players(card_id),
    platform       TEXT NOT NULL DEFAULT 'ps',
    score_type     TEXT NOT NULL,   -- investment | risk | confidence | recovery_probability |
                                     -- crash_probability | market_regime | momentum |
                                     -- supply_pressure | demand_pressure | opportunity
    value          NUMERIC(6,2) NOT NULL,
    engine_version TEXT NOT NULL,
    inputs         JSONB,
    computed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (card_id, platform, score_type, computed_at)
) PARTITION BY RANGE (computed_at);

CREATE TABLE IF NOT EXISTS card_scores_default PARTITION OF card_scores DEFAULT;

CREATE INDEX IF NOT EXISTS idx_card_scores_card_type_time ON card_scores (card_id, score_type, computed_at DESC);

CREATE MATERIALIZED VIEW IF NOT EXISTS card_scores_latest AS
SELECT DISTINCT ON (card_id, platform, score_type)
    card_id, platform, score_type, value, engine_version, inputs, computed_at
FROM card_scores
ORDER BY card_id, platform, score_type, computed_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_card_scores_latest_pk ON card_scores_latest (card_id, platform, score_type);
