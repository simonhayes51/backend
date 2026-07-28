-- Migration 019: Event Market Impact
-- target: player
-- requires-table: market_events, fut_players
--
-- Persisted, computed before/after price+volume comparison per
-- (event, card) pair. Written by a scheduled backend job
-- (app/services/event_impact.py), never by the collector - the
-- collector only knows what it scraped, not how prices moved
-- afterward. `relation` distinguishes the structurally different ways
-- one SBC can affect different cards.

CREATE TABLE IF NOT EXISTS event_market_impact (
    id                   BIGSERIAL PRIMARY KEY,
    event_id             BIGINT NOT NULL REFERENCES market_events(id) ON DELETE CASCADE,
    card_id              BIGINT NOT NULL REFERENCES fut_players(card_id),
    relation             TEXT NOT NULL,   -- 'fodder_demand' | 'reward_supply' | 'meta_shift' | 'requirement_target'
    price_before         BIGINT,
    price_after          BIGINT,
    price_change_pct     NUMERIC(6,2),
    volume_before_24h    INTEGER,
    volume_after_24h     INTEGER,
    measured_before_at   TIMESTAMPTZ,
    measured_after_at    TIMESTAMPTZ,
    computed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (event_id, card_id, relation)
);
CREATE INDEX IF NOT EXISTS idx_event_market_impact_card ON event_market_impact (card_id, computed_at DESC);
