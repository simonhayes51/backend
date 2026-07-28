-- Migration 021: AI Recommendations (versioned, engine-swappable contract)
-- target: player
-- requires-table: fut_players, market_events

CREATE TABLE IF NOT EXISTS recommendations (
    id                      BIGSERIAL PRIMARY KEY,
    card_id                 BIGINT NOT NULL REFERENCES fut_players(card_id),
    platform                TEXT NOT NULL DEFAULT 'ps',
    recommendation          TEXT NOT NULL,   -- 'buy' | 'sell' | 'hold' | 'avoid'
    confidence               NUMERIC(5,2) NOT NULL,
    expected_roi_pct         NUMERIC(6,2),
    holding_period_days      INTEGER,
    risk_rating              TEXT,           -- 'low' | 'medium' | 'high'
    reasoning                TEXT,
    market_drivers           JSONB NOT NULL DEFAULT '[]',
    similar_events           JSONB NOT NULL DEFAULT '[]',
    engine_version           TEXT NOT NULL,
    inputs                   JSONB NOT NULL DEFAULT '{}',
    outcome_actual_roi_pct   NUMERIC(6,2),
    outcome_measured_at      TIMESTAMPTZ,
    computed_at              TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_recommendations_card_time ON recommendations (card_id, platform, computed_at DESC);
CREATE INDEX IF NOT EXISTS idx_recommendations_engine ON recommendations (engine_version, computed_at DESC);

-- Addition beyond the originally-approved plan's schema design (which
-- named only the append-only table above) - added because every
-- "current recommendation for card X" read otherwise needs a
-- correlated subquery against an append-only table. Same
-- REFRESH CONCURRENTLY pattern as fair_value_mv/card_scores_latest.
CREATE MATERIALIZED VIEW IF NOT EXISTS recommendations_latest AS
SELECT DISTINCT ON (card_id, platform)
    card_id, platform, recommendation, confidence, expected_roi_pct,
    holding_period_days, risk_rating, reasoning, market_drivers,
    similar_events, engine_version, computed_at
FROM recommendations
ORDER BY card_id, platform, computed_at DESC;

CREATE UNIQUE INDEX IF NOT EXISTS idx_recommendations_latest_pk ON recommendations_latest (card_id, platform);
