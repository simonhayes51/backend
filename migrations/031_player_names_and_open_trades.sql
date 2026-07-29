BEGIN;

ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS display_name TEXT;

UPDATE fut_players
SET display_name = COALESCE(
  NULLIF(BTRIM(nickname), ''),
  NULLIF(BTRIM(card_name), ''),
  NULLIF(BTRIM(CONCAT_WS(' ', first_name, last_name)), ''),
  NULLIF(BTRIM(name), '')
)
WHERE display_name IS NULL OR BTRIM(display_name) = '';

-- Known EA legal-name/card-name mismatches. Keep these at source so every UI,
-- search result, generated image and notification uses the football name.
UPDATE fut_players SET display_name='Gilberto Silva', card_name='Gilberto Silva', nickname='Gilberto Silva'
WHERE name ILIKE '%Aparecido da Silva%' OR card_name ILIKE '%Gilberto Silva%';
UPDATE fut_players SET display_name='Nico Williams', card_name='Nico Williams', nickname='Nico Williams'
WHERE name ILIKE '%Nicholas Williams Arthuer%' OR card_name ILIKE '%Nico Williams%';
UPDATE fut_players SET display_name='Bobby Moore', card_name='Bobby Moore', nickname='Bobby Moore'
WHERE (name ILIKE '%Robert Frederick Chelsea Moore%' OR (card_name='Moore' AND version ILIKE '%Icon%'));

CREATE INDEX IF NOT EXISTS idx_fut_players_display_name ON fut_players (LOWER(display_name));

ALTER TABLE trades ADD COLUMN IF NOT EXISTS card_id BIGINT;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS bought_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS sold_at TIMESTAMPTZ;
ALTER TABLE trades ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'closed';
ALTER TABLE trades ALTER COLUMN sell DROP NOT NULL;

UPDATE trades SET bought_at=COALESCE(bought_at, timestamp), sold_at=COALESCE(sold_at, timestamp), status='closed'
WHERE sell IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_trades_user_status ON trades(user_id, status, bought_at DESC);

COMMIT;
