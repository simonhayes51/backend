-- Migration 024: Recommendation Engine V1.2 schema
-- target: player
-- requires-table: recommendations, fut_players
--
-- Extends the existing `recommendations` table (migration 021) rather
-- than replacing it - it already has the right shape for an
-- engine-swappable, backtestable evaluation log (engine_version, inputs
-- JSONB, outcome_actual_roi_pct/outcome_measured_at). V1.2 adds the
-- columns the old rule_v1 engine never had: a genuine tax-aware ROI
-- family, break-even, the five composite scores, per-strategy
-- qualification, hard-gate failure reasons, and held-position fields.
--
-- The legacy `recommendation` (lowercase buy/sell/hold/avoid) and
-- `expected_roi_pct` columns are kept for any existing reader that
-- depends on them (SELECT * is used throughout app/routers/v2/
-- recommendations.py and dashboard.py) but are DEPRECATED as of this
-- migration - the writer (Phase 4's RecommendationEngine) derives them
-- from the new correct after-tax numbers rather than the old
-- discount_pct * confidence formula (app/services/recommendation_engine.py
-- :81-98), never continuing to write a fabricated pre-tax figure under
-- expected_roi_pct. New consumers must read `status` and the returns_*
-- columns below, not the legacy pair.
--
-- Also adds three new tables for the ML pipeline this schema is meant to
-- eventually support (Phase 7 writes to these; no model is promoted by
-- this migration - ml_model_registry starts empty and the rule engine
-- remains the only possible champion_source until a real promotion
-- happens through ml_model_promotions).

ALTER TABLE recommendations
    ADD COLUMN IF NOT EXISTS status TEXT,                       -- 'BUY'|'WAIT'|'SELL'|'AVOID'|'INSUFFICIENT_DATA'
    ADD COLUMN IF NOT EXISTS entry_price BIGINT,
    ADD COLUMN IF NOT EXISTS break_even_sale_price BIGINT,
    ADD COLUMN IF NOT EXISTS conservative_price BIGINT,
    ADD COLUMN IF NOT EXISTS likely_price BIGINT,
    ADD COLUMN IF NOT EXISTS bullish_price BIGINT,
    ADD COLUMN IF NOT EXISTS potential_price BIGINT,
    ADD COLUMN IF NOT EXISTS conservative_net_roi NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS likely_net_roi NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS bullish_net_roi NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS potential_net_roi NUMERIC(10,6),
    -- Null until a validated ML champion exists (see module docstring in
    -- app/services/trading_math.py and the ML infra phase) - never a
    -- probability-weighted guess dressed up as a real forecast.
    ADD COLUMN IF NOT EXISTS expected_net_roi NUMERIC(10,6),
    ADD COLUMN IF NOT EXISTS expected_net_roi_source TEXT DEFAULT 'unavailable_until_validated_model',
    ADD COLUMN IF NOT EXISTS historical_fraction_at_or_above_likely NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS score_valuation NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS score_momentum NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS score_liquidity NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS score_risk NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS score_confidence NUMERIC(6,4),
    ADD COLUMN IF NOT EXISTS qualified_strategies JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS strategy_results JSONB NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS failed_gate_reasons JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS sales_sample_size INTEGER,
    ADD COLUMN IF NOT EXISTS sales_window TEXT,                 -- '24h'|'7d'
    ADD COLUMN IF NOT EXISTS price_age_minutes INTEGER,
    -- Held-position fields - NULL for a fresh-buy evaluation.
    ADD COLUMN IF NOT EXISTS is_held BOOLEAN NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS purchase_price BIGINT,
    ADD COLUMN IF NOT EXISTS held_decision TEXT,                -- 'SELL'|'HOLD'|'INSUFFICIENT_DATA'
    ADD COLUMN IF NOT EXISTS held_decision_reasons JSONB NOT NULL DEFAULT '[]',
    ADD COLUMN IF NOT EXISTS incremental_hold_value NUMERIC(10,6),
    -- Provenance / champion routing.
    ADD COLUMN IF NOT EXISTS requested_by TEXT NOT NULL DEFAULT 'scheduled', -- 'user'|'scheduled'
    ADD COLUMN IF NOT EXISTS champion_source TEXT NOT NULL DEFAULT 'rule_engine', -- 'rule_engine'|'ml_model'
    ADD COLUMN IF NOT EXISTS model_version TEXT;

CREATE INDEX IF NOT EXISTS idx_recommendations_status ON recommendations (status, computed_at DESC);

-- Rebuilt (DROP+CREATE, same pattern as fair_value_mv across migrations
-- 011/012/013) to expose the new columns - Postgres has no ALTER
-- MATERIALIZED VIEW for the underlying query.
DROP MATERIALIZED VIEW IF EXISTS recommendations_latest;

CREATE MATERIALIZED VIEW recommendations_latest AS
SELECT DISTINCT ON (card_id, platform)
    id, card_id, platform,
    recommendation, confidence, expected_roi_pct, holding_period_days, risk_rating,
    reasoning, market_drivers, similar_events, engine_version, computed_at,
    status, entry_price, break_even_sale_price,
    conservative_price, likely_price, bullish_price, potential_price,
    conservative_net_roi, likely_net_roi, bullish_net_roi, potential_net_roi,
    expected_net_roi, expected_net_roi_source, historical_fraction_at_or_above_likely,
    score_valuation, score_momentum, score_liquidity, score_risk, score_confidence,
    qualified_strategies, strategy_results, failed_gate_reasons,
    sales_sample_size, sales_window, price_age_minutes,
    is_held, purchase_price, held_decision, held_decision_reasons, incremental_hold_value,
    requested_by, champion_source, model_version
FROM recommendations
ORDER BY card_id, platform, computed_at DESC;

CREATE UNIQUE INDEX idx_recommendations_latest_pk ON recommendations_latest (card_id, platform);

-- =============================================================================
-- ML feature snapshots - versioned, hourly, point-in-time-safe.
-- =============================================================================
CREATE TABLE IF NOT EXISTS ml_feature_snapshots (
    id                          BIGSERIAL PRIMARY KEY,
    card_id                     BIGINT NOT NULL REFERENCES fut_players(card_id),
    platform                    TEXT NOT NULL DEFAULT 'ps',
    snapshot_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
    feature_pipeline_version    TEXT NOT NULL,
    -- The fair_value_mv.computed_at this snapshot was built from - not a
    -- separate snapshot table (none exists), but enough to audit "was
    -- this feature row built only from data that existed at snapshot_at,
    -- or did it leak something computed later."
    source_market_computed_at   TIMESTAMPTZ,

    -- Raw market fields (straight off fair_value_mv/bin_history at
    -- snapshot time).
    entry_price                 BIGINT,
    fair_value_24h              BIGINT,
    fair_value_7d                BIGINT,
    sales_24h                   INTEGER,
    sales_7d                    INTEGER,
    sales_per_hour_24h          NUMERIC(8,2),
    volatility_24h              BIGINT,
    bin_zscore_24h              NUMERIC(8,4),
    trend_falling                BOOLEAN,
    data_quality_suspect        BOOLEAN,
    price_age_minutes           INTEGER,

    -- V1.2 engineered fields.
    break_even_sale_price       BIGINT,
    conservative_price          BIGINT,
    likely_price                 BIGINT,
    bullish_price                BIGINT,
    potential_price             BIGINT,
    conservative_net_roi        NUMERIC(10,6),
    likely_net_roi              NUMERIC(10,6),
    bullish_net_roi             NUMERIC(10,6),
    potential_net_roi           NUMERIC(10,6),
    historical_fraction_at_or_above_likely NUMERIC(6,4),
    score_valuation              NUMERIC(6,4),
    score_momentum               NUMERIC(6,4),
    score_liquidity              NUMERIC(6,4),
    score_risk                   NUMERIC(6,4),
    score_confidence             NUMERIC(6,4),

    -- Card metadata (denormalized at snapshot time so a later fut_players
    -- update can't silently change what a historical feature row means).
    card_rating                  INTEGER,
    card_position                TEXT,
    card_version                 TEXT,

    -- Nullable SBC context - only populated when a real active
    -- market_events(kind='sbc') row names this card as fodder/reward;
    -- never inferred.
    sbc_context                  JSONB,

    eligibility_tier              TEXT NOT NULL DEFAULT 'INVALID', -- 'INVALID'|'MODEL_ELIGIBLE'|'LIVE_ELIGIBLE'
    would_pass_live_gates        BOOLEAN NOT NULL DEFAULT false,
    failed_gate_reasons          JSONB NOT NULL DEFAULT '[]',

    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_feature_snapshots_card_time_version
    ON ml_feature_snapshots (card_id, platform, snapshot_at, feature_pipeline_version);
CREATE INDEX IF NOT EXISTS idx_ml_feature_snapshots_eligibility
    ON ml_feature_snapshots (eligibility_tier, snapshot_at DESC);

-- =============================================================================
-- ML labels - horizon-specific outcomes per feature snapshot. Only
-- closed windows (label_closed_at IS NOT NULL) are trainable.
-- =============================================================================
CREATE TABLE IF NOT EXISTS ml_labels (
    id                            BIGSERIAL PRIMARY KEY,
    feature_snapshot_id           BIGINT NOT NULL REFERENCES ml_feature_snapshots(id) ON DELETE CASCADE,
    horizon                       TEXT NOT NULL,             -- '24h'|'48h'|'7d'
    label_policy_version          TEXT NOT NULL,

    entry_price                   BIGINT NOT NULL,           -- copied from the snapshot for leakage auditing
    strategy_target_price         BIGINT,

    realized_sale_price           BIGINT,                    -- first completed sale >= target, if any
    realized_at                   TIMESTAMPTZ,
    target_reached                BOOLEAN,
    time_to_target_minutes        INTEGER,

    mark_to_market_price          BIGINT,                    -- price at horizon close, target or not
    mark_to_market_return         NUMERIC(10,6),
    strategy_realized_return      NUMERIC(10,6),

    -- Diagnostic-only, explicitly not usable as a realised return.
    max_favourable_excursion      NUMERIC(10,6),
    max_adverse_excursion         NUMERIC(10,6),

    no_market_activity_in_window  BOOLEAN NOT NULL DEFAULT false,

    label_closed_at               TIMESTAMPTZ,               -- NULL = window not yet closed = not trainable
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_labels_snapshot_horizon_policy
    ON ml_labels (feature_snapshot_id, horizon, label_policy_version);
CREATE INDEX IF NOT EXISTS idx_ml_labels_open_windows
    ON ml_labels (horizon, label_closed_at) WHERE label_closed_at IS NULL;

-- =============================================================================
-- Model registry + an append-only promotion audit log. Promotion to
-- CHAMPION must go through a service function that writes a
-- ml_model_promotions row alongside the status UPDATE - no code path may
-- flip ml_model_registry.status directly. Nothing in this migration
-- inserts a row here; the registry starts empty.
-- =============================================================================
CREATE TABLE IF NOT EXISTS ml_model_registry (
    id                            BIGSERIAL PRIMARY KEY,
    model_version                 TEXT NOT NULL UNIQUE,
    horizon                       TEXT NOT NULL,             -- '24h'|'48h'|'7d'
    feature_pipeline_version      TEXT NOT NULL,
    training_data_start           TIMESTAMPTZ,
    training_data_end             TIMESTAMPTZ,
    feature_variant                TEXT,                      -- 'raw'|'engineered'|'combined'
    validation_metrics             JSONB NOT NULL DEFAULT '{}',
    economic_metrics               JSONB NOT NULL DEFAULT '{}',
    artifact_location               TEXT,
    status                          TEXT NOT NULL DEFAULT 'TRAINED'
        CHECK (status IN ('TRAINED','SHADOW','CHALLENGER','CHAMPION','RETIRED')),
    promoted_at                    TIMESTAMPTZ,
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Only one CHAMPION per horizon at a time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_model_registry_one_champion_per_horizon
    ON ml_model_registry (horizon) WHERE status = 'CHAMPION';

CREATE TABLE IF NOT EXISTS ml_model_promotions (
    id                             BIGSERIAL PRIMARY KEY,
    model_version                  TEXT NOT NULL REFERENCES ml_model_registry(model_version),
    from_status                    TEXT,
    to_status                      TEXT NOT NULL,
    reason                         TEXT NOT NULL,
    decided_by                     TEXT NOT NULL,             -- 'system' | a real operator identifier
    metrics_snapshot               JSONB NOT NULL DEFAULT '{}',
    created_at                     TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ml_model_promotions_model
    ON ml_model_promotions (model_version, created_at DESC);
