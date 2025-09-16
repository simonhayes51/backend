from fastapi import APIRouter, Depends, HTTPException, Query
from typing import List, Dict, Any
from main import get_player_db   # ✅ use the player pool, not app.db

router = APIRouter(prefix="/api/players", tags=["Players"])

@router.get("/search")
async def search_players(
    q: str = Query(..., min_length=2),
    limit: int = 20,
    db=Depends(get_player_db),   # ✅ switched to player DB
) -> List[Dict[str, Any]]:
    rows = await db.fetch(
        """
        SELECT card_id, name, rating, version, image_url, position
        FROM public.fut_players
        WHERE name ILIKE '%' || $1 || '%'
        ORDER BY rating DESC NULLS LAST, name ASC
        LIMIT $2
        """,
        q, limit,
    )
    return [dict(r) for r in rows]


@router.get("/resolve")
async def resolve_player_by_name(
    name: str = Query(..., min_length=2),
    db=Depends(get_player_db),   # ✅ switched to player DB
) -> Dict[str, Any]:
    row = await db.fetchrow(
        """
        SELECT card_id, name, rating, version, image_url, position
        FROM public.fut_players
        WHERE name ILIKE $1
        ORDER BY rating DESC NULLS LAST
        LIMIT 1
        """,
        name,
    )
    if not row:
        # fallback: contains match
        row = await db.fetchrow(
            """
            SELECT card_id, name, rating, version, image_url, position
            FROM public.fut_players
            WHERE name ILIKE '%' || $1 || '%'
            ORDER BY rating DESC NULLS LAST, name ASC
            LIMIT 1
            """,
            name,
        )
    if not row:
        raise HTTPException(404, "Player not found")
    return dict(row)
