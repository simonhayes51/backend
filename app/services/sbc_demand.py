# app/services/sbc_demand.py
#
# Aggregate SBC nation/league demand - the exact gap event_impact.py's own
# comment flags ("fodder_demand / meta_shift ... Intentionally not
# computed in this version"). sbc_challenges.requirements is unstructured
# free text (no typed rating/chemistry/league/nation fields), so this
# reimplements auto_sync/easysbc_sbc_sync.py's own keyword/nation-name
# matching technique query-time against live requirement text, rather than
# importing it - auto_sync and backend are separate deployments with no
# shared package, and this needs to run against currently-open SBCs on
# every dashboard read, not once at scrape time.
from __future__ import annotations

import re
from typing import Dict, List

import asyncpg


async def _reference_names(conn: asyncpg.Connection, column: str) -> List[str]:
    """Real nation/league names already in fut_players - queried at
    runtime so "Spain" or "Premier League" in a requirement string is
    matched against what's actually in this database, not a static,
    driftable list. Sorted longest-first so a multi-word name (e.g.
    "Korea Republic") is checked before a shorter name that might also
    appear as a substring of it."""
    rows = await conn.fetch(
        f"SELECT DISTINCT {column} AS value FROM fut_players WHERE {column} IS NOT NULL AND {column} <> ''"
    )
    names = [row["value"] for row in rows if row["value"]]
    names.sort(key=len, reverse=True)
    return names


def _match_name(text: str, names: List[str]) -> str | None:
    """Whole-phrase, word-boundary match - not a naive substring `in`
    check, which would false-positive (e.g. "Mali" inside "Somalia")."""
    for name in names:
        if re.search(rf"\b{re.escape(name)}\b", text, re.IGNORECASE):
            return name
    return None


async def compute_sbc_demand(player_pool: asyncpg.Pool) -> Dict[str, Dict[str, int]]:
    """One pass over every currently-open SBC's requirement text.
    Returns {"nation": {name: count}, "league": {name: count}} - how many
    live SBCs currently name each nation/league in a requirement."""
    async with player_pool.acquire() as conn:
        nations = await _reference_names(conn, "nation")
        leagues = await _reference_names(conn, "league")

        rows = await conn.fetch(
            """
            SELECT c.requirements
            FROM sbc_challenges c
            JOIN market_events e ON e.id = c.event_id
            WHERE e.kind = 'sbc' AND (e.ends_at IS NULL OR e.ends_at > now())
            """
        )

    nation_counts: Dict[str, int] = {}
    league_counts: Dict[str, int] = {}

    for row in rows:
        requirements = row["requirements"] or {}
        if not isinstance(requirements, dict):
            continue
        text = " ".join(str(v) for v in requirements.values())

        nation = _match_name(text, nations)
        if nation:
            nation_counts[nation] = nation_counts.get(nation, 0) + 1

        league = _match_name(text, leagues)
        if league:
            league_counts[league] = league_counts.get(league, 0) + 1

    return {"nation": nation_counts, "league": league_counts}


async def sbc_demand_for_card(player_pool: asyncpg.Pool, card_id: int) -> Dict[str, int]:
    """Convenience lookup for a single card: how many currently-open SBCs
    name this card's own nation/league. Recomputes the full aggregate each
    call rather than caching - callers needing this at scale (a full
    dashboard pass) should call compute_sbc_demand() once and look up
    both fields themselves instead."""
    async with player_pool.acquire() as conn:
        card = await conn.fetchrow("SELECT nation, league FROM fut_players WHERE card_id = $1", card_id)
    if card is None:
        return {"nation_sbc_count": 0, "league_sbc_count": 0}

    demand = await compute_sbc_demand(player_pool)
    return {
        "nation_sbc_count": demand["nation"].get(card["nation"], 0),
        "league_sbc_count": demand["league"].get(card["league"], 0),
    }
