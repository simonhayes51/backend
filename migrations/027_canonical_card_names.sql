-- target: player
-- Make the short in-game card name the canonical display name everywhere.
-- Existing rows often have the correct public name in `nickname` while `name`
-- contains the player's full legal name. APIs already expose `card_name`, so
-- populate it once and keep it populated for future imports.

UPDATE fut_players
SET card_name = COALESCE(
    NULLIF(BTRIM(card_name), ''),
    NULLIF(BTRIM(nickname), ''),
    NULLIF(BTRIM(name), '')
)
WHERE card_name IS NULL OR BTRIM(card_name) = '';

CREATE OR REPLACE FUNCTION set_fut_player_card_name()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    NEW.card_name := COALESCE(
        NULLIF(BTRIM(NEW.card_name), ''),
        NULLIF(BTRIM(NEW.nickname), ''),
        NULLIF(BTRIM(NEW.name), '')
    );
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS fut_players_set_card_name ON fut_players;
CREATE TRIGGER fut_players_set_card_name
BEFORE INSERT OR UPDATE OF card_name, nickname, name
ON fut_players
FOR EACH ROW
EXECUTE FUNCTION set_fut_player_card_name();

-- Keep the player-facing `name` field aligned as well. This prevents legacy
-- endpoints/components that still read `name` from showing legal names such as
-- "Aparecido da Silva" or "Nicholas Williams Arthuer".
UPDATE fut_players
SET name = card_name
WHERE card_name IS NOT NULL
  AND BTRIM(card_name) <> ''
  AND name IS DISTINCT FROM card_name;
