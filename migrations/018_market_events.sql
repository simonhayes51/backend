-- Migration 018: Market Events (generic event ingestion, SBC first)
-- target: player
-- requires-table: fut_players
--
-- NOT the same as the existing core-DB `events` table
-- (010_consolidate_core_bootstrap.sql, a narrow "Next Promo" calendar
-- row keyed by name/kind/start_at). This is a different table on a
-- different database: card-scoped, fingerprinted, JSONB-payload'd,
-- designed to carry SBCs today and promos/objectives/store-packs/
-- evolutions later without ever needing another migration for a new
-- `kind`.

CREATE TABLE IF NOT EXISTS market_events (
    id            BIGSERIAL PRIMARY KEY,
    kind          TEXT NOT NULL,                  -- 'sbc' | 'promo' | 'objective' | ...
    source        TEXT NOT NULL,                  -- 'futbin' | ...
    external_id   TEXT NOT NULL,                  -- source's own natural key (e.g. futbin set id)
    title         TEXT NOT NULL,
    description   TEXT,
    starts_at     TIMESTAMPTZ,
    ends_at       TIMESTAMPTZ,
    fingerprint   TEXT[] NOT NULL DEFAULT '{}',   -- e.g. requires_totw, high_fodder_demand, icon_reward
    payload       JSONB NOT NULL DEFAULT '{}',
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (kind, source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_market_events_fingerprint ON market_events USING GIN (fingerprint);
CREATE INDEX IF NOT EXISTS idx_market_events_kind_starts ON market_events (kind, starts_at DESC);

-- 1:1 SBC-specific fields, off the generic event row.
CREATE TABLE IF NOT EXISTS sbc_details (
    event_id            BIGINT PRIMARY KEY REFERENCES market_events(id) ON DELETE CASCADE,
    set_name            TEXT NOT NULL,
    category            TEXT,              -- 'icon' | 'hero' | 'upgrade' | 'foundations' | ...
    total_cost_coins    BIGINT,
    repeatable          BOOLEAN NOT NULL DEFAULT false,
    reward_card_id      BIGINT REFERENCES fut_players(card_id),
    reward_description  TEXT,
    expires_at          TIMESTAMPTZ
);

-- 1:many challenge breakdown.
CREATE TABLE IF NOT EXISTS sbc_challenges (
    id                    BIGSERIAL PRIMARY KEY,
    event_id              BIGINT NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    challenge_name        TEXT NOT NULL,
    requirements          JSONB NOT NULL DEFAULT '{}',  -- e.g. {"min_rating":83,"chem_min":18}
    estimated_cost_coins  BIGINT,
    display_order         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sbc_challenges_event ON sbc_challenges (event_id);
