-- Migration 026: manual/automatic flagging for bad generated player cards
-- target: player
-- requires-table: fut_players
--
-- Backs two symptoms seen in production: (1) a handful of generated cards
-- show only the player's face crop because the background/cutout layers
-- never actually finished loading before the "ready" screenshot was taken
-- (see player_card_render.py's cardDegraded check), and (2) a handful show
-- a completely different player's card. generated_card_flagged marks a
-- ready card as known-bad without immediately clobbering its (still-served)
-- URL - app/services/player_card_ondemand.py's claim query picks up any
-- flagged+ready card on the next request for it and forces a real
-- regeneration, the same retry path as an 'error' status. No separate
-- audit table - git history + generated_card_flag_reason is enough.

ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_flagged BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_flag_reason TEXT;
ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS generated_card_flagged_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_fut_players_generated_card_flagged
    ON fut_players (generated_card_flagged)
    WHERE generated_card_flagged;
