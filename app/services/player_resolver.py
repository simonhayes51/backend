# app/services/player_resolver.py
#
# Parses a natural-language card reference like "Mbappe 92" into a
# card_id, for the AI chat's resolve_and_evaluate_card tool. Extends
# (doesn't replace) app/routers/players.py's /api/players/resolve, which
# already does name-only exact/fuzzy matching with a "highest rated"
# fallback - that fallback is exactly wrong for a chat query naming a
# specific rating, so this adds a rating-aware path and disambiguates
# with a candidate list instead of silently guessing when a name matches
# more than one card.
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional

import asyncpg

_TRAILING_RATING_RE = re.compile(r"^(.*?)\s+(\d{2,3})$")


@dataclass
class ResolvedCard:
    card_id: int
    name: str
    rating: int
    version: str


@dataclass
class ResolutionResult:
    card: Optional[ResolvedCard] = None
    candidates: List[ResolvedCard] = field(default_factory=list)
    query_name: str = ""
    query_rating: Optional[int] = None


def _split_name_and_rating(query: str) -> tuple[str, Optional[int]]:
    """"Mbappe 92" -> ("Mbappe", 92). "Mbappe" -> ("Mbappe", None). A
    trailing 2-3 digit number is treated as a rating, never a jersey
    number or other digit string, since FUT ratings are always in that
    range and nothing else in a player query legitimately looks like it."""
    query = query.strip()
    match = _TRAILING_RATING_RE.match(query)
    if match:
        name, rating_str = match.groups()
        rating = int(rating_str)
        if 40 <= rating <= 99:
            return name.strip(), rating
    return query, None


async def resolve_card(conn: asyncpg.Connection, query: str) -> ResolutionResult:
    """Resolves a chat query to exactly one card, a disambiguation list,
    or nothing - never a guess when more than one real candidate exists."""
    name, rating = _split_name_and_rating(query)
    result = ResolutionResult(query_name=name, query_rating=rating)
    if not name:
        return result

    params: list = [f"%{name}%"]
    if rating is not None:
        params.append(rating)
        sql = """
            SELECT card_id, name, rating, version
            FROM fut_players
            WHERE LOWER(name) ILIKE LOWER($1) AND rating = $2
            ORDER BY rating DESC
            LIMIT 6
        """
    else:
        sql = """
            SELECT card_id, name, rating, version
            FROM fut_players
            WHERE LOWER(name) ILIKE LOWER($1)
            ORDER BY (LOWER(name) = LOWER($1)) DESC, rating DESC
            LIMIT 6
        """

    rows = await conn.fetch(sql, *params)

    candidates = [
        ResolvedCard(card_id=int(r["card_id"]), name=r["name"], rating=r["rating"], version=r["version"])
        for r in rows
    ]

    if not candidates:
        return result

    # An exact rating in the query is decisive by itself; without one,
    # only a single real candidate is safe to resolve automatically -
    # more than one is a genuine ambiguity (e.g. multiple versions of the
    # same player), not something to silently pick the "best" of.
    if rating is not None or len(candidates) == 1:
        result.card = candidates[0]
    else:
        result.candidates = candidates

    return result
