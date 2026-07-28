-- Migration 022: card-art layer columns on fut_players
-- target: player
-- requires-table: fut_players
--
-- bgImageUrl/cutoutImageUrl/cutoutType/cardName are currently only ever
-- produced by a LIVE, uncached, per-request scrape of a card's futbin.com
-- player page (app/futbin_client.py's fetch_card_layers/parse_card_layers,
-- called from GET /api/fut-player-definition/{card_id}). That's fine for a
-- single Player Page detail view but not viable for list surfaces (v2 Home
-- Dashboard's Movers/Opportunities/High-Confidence/Avoid/Watchlist/Activity,
-- SBC Hub/Event Detail). Nullable, no default - backfilled gradually by a
-- new auto_sync worker (futbin_card_art_backfill.py), same non-blocking
-- philosophy as every other additive migration in this project. NULL is a
-- legitimate, expected steady state for low-traffic cards for a long time;
-- every consumer of these columns must fall back to fut_players.image_url
-- when they're still null, not assume they're always populated.

ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS card_bg_image TEXT;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS card_cutout_image TEXT;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS card_cutout_type TEXT; -- 'base' | 'special'
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS card_name TEXT;
