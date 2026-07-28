-- Migration 017: fut_players bootstrap
-- target: player
--
-- fut_players' schema authority is split three ways today: auto_sync's
-- futbin_full_sync.py only INSERTs/ALTERs it (never creates it),
-- backend/scripts/refresh_all_prices_loop.py has its own minimal
-- CREATE TABLE but runs it against DATABASE_URL (core), not
-- PLAYER_DATABASE_URL, and no migration in this repo creates it at all.
-- Migrations 018+ need a real FK target for fut_players on the PLAYER
-- database, so this lands that bootstrap first. True no-op everywhere
-- fut_players already exists with more columns - IF NOT EXISTS only
-- guarantees the table + primary key, never touches existing columns.

CREATE TABLE IF NOT EXISTS fut_players (
    card_id BIGINT PRIMARY KEY
);
