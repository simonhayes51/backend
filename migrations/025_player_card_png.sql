-- Migration 025: generated player-card PNG export columns on fut_players
-- target: player
-- requires-table: fut_players
--
-- Backing store for the server-rendered, flattened "standalone card" PNG
-- (background + cutout + rating/stats/badges baked into one transparent
-- image, similar in purpose to FUTBIN's mobile card export) uploaded to
-- S3-compatible object storage by app/services/player_card_generation.py.
-- Nullable, no default - populated lazily on first admin-triggered
-- generation or by the bulk backfill script (scripts/generate_player_cards.py),
-- same non-blocking additive-migration philosophy as migration 022's card
-- art layer columns. generated_card_hash lets ensure_generated_player_card()
-- skip re-rendering when nothing that affects the card's appearance has
-- changed since the last successful generation.

ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_url TEXT;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_key TEXT;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_hash VARCHAR(64);
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_width INTEGER;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_height INTEGER;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_at TIMESTAMPTZ;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_status VARCHAR(20);
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_error TEXT;

-- Fast lookup for the bulk "--missing" backfill mode (status IS NULL or
-- 'error') without a full table scan on large catalogs.
CREATE INDEX IF NOT EXISTS idx_fut_players_generated_card_status
    ON fut_players (generated_card_status);
