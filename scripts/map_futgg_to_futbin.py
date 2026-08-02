"""
Populate card_source_map (migrations/038) by cross-referencing the new
FUT.GG catalogue (futgg_players, core DB) against the legacy FUTBIN
catalogue (fut_players, player DB) on name + rating + rarity.

This is a READ-ONLY match against both catalogues and an ADDITIVE write to
card_source_map only - it never touches fut_players, futgg_players, or any
price/sales history table, and never overwrites a FUT.GG card's own
source_card_id with a FUTBIN id anywhere. The two catalogues stay on
separate live Postgres instances (fut_players is target:player,
futgg_players is target:core - see migrations 017/025 vs 038's own
requires-table comment), so the match happens in Python, not SQL.

Matching, cheapest-first:
  1. Normalize name (casefold, strip diacritics/punctuation/whitespace)
     and require an exact rating match. This is the join key.
  2. If exactly one FUTBIN card shares that (name, rating) key, it's a
     match. Rarity/position/club agreement raises confidence; a mismatch
     on those doesn't cancel the match (FUTBIN and FUT.GG use different
     rarity vocabularies - "TOTS" vs "Team of the Season" - so this is
     corroborating evidence, not a hard gate) but keeps confidence lower
     and reviewed=False.
  3. If MORE than one FUTBIN card shares that (name, rating) key
     (duplicate-name collisions, or genuinely different cards that
     happen to share both), try to disambiguate with rarity, then
     position, then club. If it's still ambiguous after that, the card
     is SKIPPED - never guessed. A wrong pairing silently pollutes a
     card's history with a different player's data, which is worse than
     leaving it unmapped (see the task's own "never fuzzy-match as a
     primary key / never silently merge" rule).

Confident matches (unique key, rarity agrees) get reviewed=True and
written immediately - this script auto-populates rather than gating
behind a manual review step (explicit instruction: partial automatic
coverage now is better than zero coverage, everything here is written to
a bridge table that never overwrites either source, so a bad row is
cheap to correct or delete later without touching real data).

Usage:
    python scripts/map_futgg_to_futbin.py                  # apply
    python scripts/map_futgg_to_futbin.py --dry-run         # report only

Requires DATABASE_URL (core - card_source_map + futgg_players) and
PLAYER_DATABASE_URL (fut_players) both set.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from typing import Optional

import asyncpg


def normalize_name(value: Optional[str]) -> str:
    if not value:
        return ""
    decomposed = unicodedata.normalize("NFD", value)
    stripped = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]", "", stripped.casefold())


def normalize_rarity(value: Optional[str]) -> str:
    if not value:
        return ""
    text = re.sub(r"[^a-z0-9]", "", value.casefold())
    # FUTBIN and FUT.GG vocabularies diverge on common promos - collapse
    # both sides onto the same bucket for the ones common enough to be
    # worth it. Anything not listed here just compares as its own
    # normalized text (still useful when both sides happen to agree,
    # e.g. "rare"/"rare", "icon"/"icon").
    aliases = {
        "totw": "teamoftheweek", "teamoftheweek": "teamoftheweek",
        "tots": "teamoftheseason", "teamoftheseason": "teamoftheseason",
        "toty": "teamoftheyear", "teamoftheyear": "teamoftheyear",
        "otw": "onestowatch", "onestowatch": "onestowatch",
        "futties": "futties",
        "normal": "base", "common": "base", "base": "base",
    }
    return aliases.get(text, text)


@dataclass
class FutbinCard:
    card_id: int
    name: str
    rating: int
    rarity: Optional[str]
    position: Optional[str]
    club: Optional[str]


@dataclass
class FutggCard:
    source_card_id: int
    name: str
    rating: int
    rarity: Optional[str]
    position: Optional[str]
    club: Optional[str]


@dataclass
class MatchResult:
    futgg_source_card_id: int
    futbin_card_id: int
    match_method: str
    match_confidence: float
    reviewed: bool


async def fetch_futbin_cards(dsn: str) -> list[FutbinCard]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT card_id,
                   COALESCE(display_name, card_name, name) AS display_name,
                   rating, rarity, position, club
            FROM fut_players
            WHERE rating IS NOT NULL
              AND COALESCE(display_name, card_name, name) IS NOT NULL
            """
        )
    finally:
        await conn.close()
    return [
        FutbinCard(r["card_id"], r["display_name"], r["rating"], r["rarity"], r["position"], r["club"])
        for r in rows
    ]


async def fetch_futgg_cards(dsn: str) -> list[FutggCard]:
    conn = await asyncpg.connect(dsn)
    try:
        rows = await conn.fetch(
            """
            SELECT source_card_id, name, rating, rarity, primary_position, club
            FROM futgg_players
            WHERE is_active AND rating IS NOT NULL AND name IS NOT NULL
            """
        )
    finally:
        await conn.close()
    return [
        FutggCard(r["source_card_id"], r["name"], r["rating"], r["rarity"], r["primary_position"], r["club"])
        for r in rows
    ]


def build_matches(futbin_cards: list[FutbinCard], futgg_cards: list[FutggCard]) -> tuple[list[MatchResult], int, int]:
    index: dict[tuple[str, int], list[FutbinCard]] = defaultdict(list)
    for card in futbin_cards:
        index[(normalize_name(card.name), card.rating)].append(card)

    matches: list[MatchResult] = []
    skipped_ambiguous = 0
    unmatched = 0

    for card in futgg_cards:
        candidates = index.get((normalize_name(card.name), card.rating), [])
        if not candidates:
            unmatched += 1
            continue

        chosen = candidates
        if len(chosen) > 1:
            by_rarity = [c for c in chosen if normalize_rarity(c.rarity) == normalize_rarity(card.rarity) and normalize_rarity(card.rarity)]
            if len(by_rarity) == 1:
                chosen = by_rarity
            else:
                by_position = [c for c in chosen if c.position and c.position == card.position]
                if len(by_position) == 1:
                    chosen = by_position
                else:
                    by_club = [c for c in chosen if c.club and c.club == card.club]
                    if len(by_club) == 1:
                        chosen = by_club

        if len(chosen) != 1:
            skipped_ambiguous += 1
            continue

        match = chosen[0]
        rarity_agrees = bool(normalize_rarity(match.rarity)) and normalize_rarity(match.rarity) == normalize_rarity(card.rarity)
        was_disambiguated = len(candidates) > 1

        if rarity_agrees and not was_disambiguated:
            method, confidence, reviewed = "name_rating_rarity", 0.95, True
        elif rarity_agrees and was_disambiguated:
            method, confidence, reviewed = "name_rating_rarity_disambiguated", 0.85, True
        elif was_disambiguated:
            method, confidence, reviewed = "name_rating_disambiguated_by_position_or_club", 0.55, False
        else:
            method, confidence, reviewed = "name_rating_only", 0.70, False

        matches.append(MatchResult(card.source_card_id, match.card_id, method, confidence, reviewed))

    return matches, unmatched, skipped_ambiguous


async def write_matches(dsn: str, matches: list[MatchResult]) -> None:
    conn = await asyncpg.connect(dsn)
    try:
        await conn.executemany(
            """
            INSERT INTO card_source_map (futgg_source_card_id, futbin_card_id, match_method, match_confidence, reviewed)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (futgg_source_card_id) DO UPDATE SET
                futbin_card_id = EXCLUDED.futbin_card_id,
                match_method = EXCLUDED.match_method,
                match_confidence = EXCLUDED.match_confidence,
                reviewed = EXCLUDED.reviewed
            """,
            [(m.futgg_source_card_id, m.futbin_card_id, m.match_method, m.match_confidence, m.reviewed) for m in matches],
        )
    finally:
        await conn.close()


async def run(dry_run: bool) -> int:
    core_dsn = os.getenv("DATABASE_URL")
    player_dsn = os.getenv("PLAYER_DATABASE_URL")
    if not core_dsn or not player_dsn:
        print("Both DATABASE_URL (core) and PLAYER_DATABASE_URL are required.", file=sys.stderr)
        return 1

    print("Fetching FUTBIN catalogue (player DB)...")
    futbin_cards = await fetch_futbin_cards(player_dsn)
    print(f"  {len(futbin_cards)} FUTBIN cards")

    print("Fetching FUT.GG catalogue (core DB)...")
    futgg_cards = await fetch_futgg_cards(core_dsn)
    print(f"  {len(futgg_cards)} FUT.GG cards")

    matches, unmatched, skipped_ambiguous = build_matches(futbin_cards, futgg_cards)

    by_method: dict[str, int] = defaultdict(int)
    for m in matches:
        by_method[m.match_method] += 1

    total = len(futgg_cards) or 1
    print(f"\nMatched:            {len(matches)} ({len(matches) / total:.0%})")
    for method, count in sorted(by_method.items(), key=lambda kv: -kv[1]):
        print(f"  - {method}: {count}")
    print(f"Unmatched (no hit): {unmatched} ({unmatched / total:.0%})")
    print(f"Skipped (ambiguous, never guessed): {skipped_ambiguous} ({skipped_ambiguous / total:.0%})")

    if dry_run:
        print("\n--dry-run: not writing to card_source_map.")
        return 0

    print(f"\nWriting {len(matches)} rows to card_source_map...")
    await write_matches(core_dsn, matches)
    print("Done.")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Report match counts without writing to card_source_map")
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args.dry_run)))
