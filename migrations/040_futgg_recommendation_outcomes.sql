-- Migration 040: FUT.GG recommendation snapshots + outcome grading
-- target: core
-- requires-table: futgg_players, futgg_bin_history, futgg_sales_history
--
-- WHY
-- ---
-- The existing ML validation loop (ml_feature_snapshots / ml_labels, fed
-- by app/services/ml_feature_pipeline.py) reads exclusively from
-- fair_value_mv - the legacy FUTBIN materialized view. That pipeline is
-- broken on the current player database and no longer drives a single
-- user-visible recommendation. The consequence is that the FUT.GG engine,
-- which drives ALL live recommendations, has zero closed-outcome
-- feedback: it emits falsifiable claims ("expected profit 3,580 after
-- tax, confidence 0.62") with no mechanism anywhere to find out whether
-- they were right.
--
-- These two tables are that mechanism. They are deliberately independent
-- of ml_feature_snapshots/ml_labels rather than an extension of them -
-- the legacy tables are keyed on FUTBIN card_id and carry FUTBIN feature
-- semantics, and conflating the two id spaces is exactly the class of bug
-- this migration exists to stop repeating.
--
-- Purely additive. Nothing here alters or drops an existing object, and
-- no user-visible endpoint depends on these tables until the track-record
-- API is repointed, so this can ship and backfill quietly.

-- ---------------------------------------------------------------------
-- 1. Recommendation snapshots - what the engine concluded, and why
-- ---------------------------------------------------------------------
-- One row per (card, evaluation). Every number the engine displayed is
-- frozen here at write time. Nothing in this table is ever recomputed
-- from current data: the whole point is to be able to ask "what did we
-- actually say at 14:05 last Tuesday", which is unanswerable if the row
-- silently re-derives itself against today's market.
CREATE TABLE IF NOT EXISTS futgg_recommendation_snapshots (
    id                        BIGSERIAL PRIMARY KEY,
    source_card_id            BIGINT NOT NULL,
    evaluated_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- ---- Market state at evaluation time ----------------------------
    current_bin               INTEGER,
    bin_captured_at           TIMESTAMPTZ,
    price_age_minutes         INTEGER,
    sales_median              NUMERIC,
    sales_trimmed_mean        NUMERIC,
    sales_count               INTEGER NOT NULL DEFAULT 0,
    sales_window_earliest_at  TIMESTAMPTZ,
    sales_window_latest_at    TIMESTAMPTZ,
    sales_window_span_minutes NUMERIC,
    sales_dispersion_ratio    NUMERIC,
    latest_sale_price         INTEGER,
    price_tier                TEXT,
    rating                    INTEGER,
    rarity                    TEXT,

    -- ---- What the engine concluded ----------------------------------
    fair_value                NUMERIC,
    theoretical_max_buy       NUMERIC,
    recommended_buy_max       NUMERIC,
    current_executable_buy    NUMERIC,
    break_even_price          NUMERIC,
    recommended_sell_target   NUMERIC,
    buy_below                 NUMERIC,
    expected_profit_after_tax NUMERIC,
    expected_roi              NUMERIC,
    confidence_score          NUMERIC,
    liquidity_score           NUMERIC,
    risk_level                TEXT,
    signal                    TEXT NOT NULL,
    status                    TEXT NOT NULL,

    -- ---- Trend ------------------------------------------------------
    -- Features are stored whole so a later analysis can ask which
    -- individual feature carried predictive weight, not merely whether
    -- the final state was right.
    trend_state               TEXT,
    trend_features            JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- ---- Gating / provenance ----------------------------------------
    reason_codes              TEXT[] NOT NULL DEFAULT '{}',
    blocking_codes            TEXT[] NOT NULL DEFAULT '{}',
    reasons                   JSONB NOT NULL DEFAULT '[]'::jsonb,
    engine_version            TEXT NOT NULL,
    trend_version             TEXT NOT NULL,
    engine_config             JSONB NOT NULL DEFAULT '{}'::jsonb,

    -- ---- Lifecycle --------------------------------------------------
    expires_at                TIMESTAMPTZ,
    expiry_minutes            INTEGER,
    -- Set by the lifecycle checker when the market moves away from the
    -- state that produced the call. Distinct from mere expiry: expired
    -- means "too old to trust", invalidated means "we know it is wrong".
    invalidated_at            TIMESTAMPTZ,
    invalidated_reason        TEXT
);

-- The grader walks ungraded rows oldest-first; the API groups by version
-- and by card. Both are covered here.
CREATE INDEX IF NOT EXISTS futgg_rec_snap_card_time_idx
    ON futgg_recommendation_snapshots (source_card_id, evaluated_at DESC);
CREATE INDEX IF NOT EXISTS futgg_rec_snap_time_idx
    ON futgg_recommendation_snapshots (evaluated_at DESC);
CREATE INDEX IF NOT EXISTS futgg_rec_snap_engine_version_idx
    ON futgg_recommendation_snapshots (engine_version, evaluated_at DESC);
-- Partial index: only actionable calls are ever graded, and they are a
-- small minority of all evaluations.
CREATE INDEX IF NOT EXISTS futgg_rec_snap_actionable_idx
    ON futgg_recommendation_snapshots (evaluated_at)
    WHERE signal IN ('buy', 'strong_buy');

-- One evaluation per card per minute is plenty; this also makes the
-- writer safely idempotent under retries without needing a transaction
-- spanning the whole scan.
--
-- The `AT TIME ZONE 'UTC'` is load-bearing, not decoration. evaluated_at
-- is TIMESTAMPTZ, and date_trunc(text, timestamptz) is STABLE rather
-- than IMMUTABLE - its result depends on the session TimeZone - so
-- Postgres rejects it in an index expression with "functions in index
-- expression must be marked IMMUTABLE". The runner applies each
-- migration inside a transaction, so that single error rolled back this
-- entire file: both tables above silently failed to exist in production
-- while the scanner kept trying to write to them every 10 minutes.
--
-- `AT TIME ZONE 'UTC'` yields a plain timestamp and is immutable, and
-- date_trunc(text, timestamp) is immutable in turn. UTC is also the only
-- defensible choice regardless - the bucket must not shift with whatever
-- TimeZone a given connection happens to carry.
--
-- The ON CONFLICT inference in futgg_recommendation_store._INSERT must
-- match this expression exactly or Postgres will not recognise the index.
CREATE UNIQUE INDEX IF NOT EXISTS futgg_rec_snap_card_minute_uniq
    ON futgg_recommendation_snapshots
       (source_card_id, date_trunc('minute', evaluated_at AT TIME ZONE 'UTC'));

COMMENT ON TABLE futgg_recommendation_snapshots IS
    'Immutable record of every actionable FUT.GG recommendation and the market state that produced it. Never recomputed from current data.';

-- ---------------------------------------------------------------------
-- 2. Outcomes - what actually happened next
-- ---------------------------------------------------------------------
-- One row per (snapshot, horizon). Horizons are 24h / 48h / 7d.
--
-- GRADING RULES (see app/services/futgg_outcome_grader.py for the
-- implementation; they are restated here because the schema only makes
-- sense alongside them):
--
--   * Grading walks futgg_bin_history CHRONOLOGICALLY. It never takes
--     the best price in the window. "Would this have worked" is a
--     question about what a user could have done at the time, in order,
--     not about the most favourable point visible in hindsight.
--
--   * ENTRY is achieved at the FIRST observation at or below the
--     recommendation's executable buy price. If the price never traded
--     there, the recommendation is graded 'no_entry' and contributes to
--     the entry rate but not to the profit statistics - counting a
--     never-purchasable call as a winner or a loser would both be wrong.
--
--   * EXIT is achieved at the first observation at or above the sell
--     target that occurs STRICTLY AFTER the entry timestamp. Ordering is
--     enforced in SQL/Python, not assumed.
--
--   * MFE/MAE are measured only over the post-entry window, for the same
--     reason: excursions before you owned the card are not yours.
CREATE TABLE IF NOT EXISTS futgg_recommendation_outcomes (
    id                     BIGSERIAL PRIMARY KEY,
    snapshot_id            BIGINT NOT NULL
        REFERENCES futgg_recommendation_snapshots(id) ON DELETE CASCADE,
    source_card_id         BIGINT NOT NULL,
    horizon                TEXT NOT NULL CHECK (horizon IN ('24h', '48h', '7d')),

    graded_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
    window_start           TIMESTAMPTZ NOT NULL,
    window_end             TIMESTAMPTZ NOT NULL,
    -- How many chronological observations backed this grade. A grade
    -- resting on two observations is not the same evidence as one
    -- resting on two hundred, and the API surfaces this.
    observation_count      INTEGER NOT NULL DEFAULT 0,

    -- ---- Entry ------------------------------------------------------
    entry_achieved         BOOLEAN NOT NULL DEFAULT FALSE,
    entry_at               TIMESTAMPTZ,
    entry_price            INTEGER,

    -- ---- Exit -------------------------------------------------------
    exit_achieved          BOOLEAN NOT NULL DEFAULT FALSE,
    exit_at                TIMESTAMPTZ,
    realised_sell_price    INTEGER,

    -- ---- Result (NULL unless entry was achieved) --------------------
    net_profit_after_tax   NUMERIC,
    realised_roi           NUMERIC,
    -- Best and worst the position got to after entry, as fractions of
    -- the entry price. MAE is the drawdown a user would actually have
    -- had to sit through - the number that decides whether a strategy is
    -- psychologically survivable, not just profitable on paper.
    max_favourable_excursion NUMERIC,
    max_adverse_excursion    NUMERIC,

    target_hit             BOOLEAN NOT NULL DEFAULT FALSE,
    downside_hit           BOOLEAN NOT NULL DEFAULT FALSE,
    minutes_to_target      INTEGER,

    -- no_entry | target_hit | profitable_unrealised | flat |
    -- loss_unrealised | downside_hit | insufficient_observations
    outcome_status         TEXT NOT NULL,

    grader_version         TEXT NOT NULL,

    UNIQUE (snapshot_id, horizon)
);

CREATE INDEX IF NOT EXISTS futgg_rec_outcome_card_idx
    ON futgg_recommendation_outcomes (source_card_id, graded_at DESC);
CREATE INDEX IF NOT EXISTS futgg_rec_outcome_horizon_idx
    ON futgg_recommendation_outcomes (horizon, graded_at DESC);
CREATE INDEX IF NOT EXISTS futgg_rec_outcome_status_idx
    ON futgg_recommendation_outcomes (outcome_status);

COMMENT ON TABLE futgg_recommendation_outcomes IS
    'Chronologically graded result per recommendation per horizon. Never uses the best future price with hindsight - entry must precede exit in observation order.';
