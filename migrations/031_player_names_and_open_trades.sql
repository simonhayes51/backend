-- Run this migration against PLAYER_DATABASE_URL only.
BEGIN;

ALTER TABLE fut_players ADD COLUMN IF NOT EXISTS display_name TEXT;

CREATE TABLE IF NOT EXISTS player_name_overrides (
  id BIGSERIAL PRIMARY KEY,
  match_name TEXT,
  match_card_name TEXT,
  match_card_id BIGINT,
  display_name TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CHECK (match_name IS NOT NULL OR match_card_name IS NOT NULL OR match_card_id IS NOT NULL)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_player_name_override_card_id
  ON player_name_overrides(match_card_id) WHERE match_card_id IS NOT NULL;

INSERT INTO player_name_overrides (match_name, display_name)
VALUES
  ('Aparecido da Silva', 'Gilberto Silva'),
  ('Nicholas Williams Arthuer', 'Nico Williams'),
  ('Robert Frederick Chelsea Moore', 'Bobby Moore')
ON CONFLICT DO NOTHING;

UPDATE fut_players p
SET display_name = COALESCE(
  (
    SELECT o.display_name
    FROM player_name_overrides o
    WHERE (o.match_card_id IS NOT NULL AND o.match_card_id = p.card_id)
       OR (o.match_name IS NOT NULL AND p.name ILIKE '%' || o.match_name || '%')
       OR (o.match_card_name IS NOT NULL AND p.card_name ILIKE '%' || o.match_card_name || '%')
    ORDER BY (o.match_card_id IS NOT NULL) DESC, o.id
    LIMIT 1
  ),
  NULLIF(BTRIM(p.nickname), ''),
  NULLIF(BTRIM(p.card_name), ''),
  NULLIF(BTRIM(CONCAT_WS(' ', p.first_name, p.last_name)), ''),
  NULLIF(BTRIM(p.name), '')
);

-- Keep the public card fields aligned for known exceptions so legacy screens,
-- generated card images and search results also receive the canonical name.
UPDATE fut_players p
SET card_name = o.display_name,
    nickname = o.display_name,
    display_name = o.display_name
FROM player_name_overrides o
WHERE (o.match_card_id IS NOT NULL AND o.match_card_id = p.card_id)
   OR (o.match_name IS NOT NULL AND p.name ILIKE '%' || o.match_name || '%')
   OR (o.match_card_name IS NOT NULL AND p.card_name ILIKE '%' || o.match_card_name || '%');

CREATE INDEX IF NOT EXISTS idx_fut_players_display_name ON fut_players (LOWER(display_name));
CREATE INDEX IF NOT EXISTS idx_player_name_overrides_match_name ON player_name_overrides (LOWER(match_name));

COMMIT;
