from fastapi import APIRouter, Depends, Query
from typing import Optional

from app.db import get_db  # <-- returns an asyncpg.Connection

router = APIRouter(prefix="/api/sbc", tags=["sbc"])

@router.get("/sets")
async def list_sets(
    pg = Depends(get_db),
    q: Optional[str] = Query(None, description="Search by set name (ILIKE)"),
    category_id: Optional[int] = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    sql = """
      SELECT
        set_id, category_id, name, description, challenges_count,
        hidden, repeatable, repeatability_mode, challenges_completed,
        end_time, release_time, set_image_id
      FROM sbc_sets
      WHERE ($1::text IS NULL OR name ILIKE '%'||$1||'%')
        AND ($2::int  IS NULL OR category_id = $2)
      ORDER BY release_time DESC NULLS LAST, set_id DESC
      LIMIT $3 OFFSET $4
    """
    rows = await pg.fetch(sql, q, category_id, limit, offset)
    return {"ok": True, "data": [dict(r) for r in rows]}

@router.get("/challenges")
async def list_challenges(
    pg = Depends(get_db),
    set_id: Optional[int] = Query(None),
    code: Optional[str] = Query(None, alias="challenge_code"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    sql = """
      SELECT
        challenge_code, set_id, name, positions, min_squad_rating, min_chem,
        min_nations, min_leagues, min_clubs, formation, constraints_json, updated_at
      FROM sbc_challenges
      WHERE ($1::int  IS NULL OR set_id = $1)
        AND ($2::text IS NULL OR challenge_code = $2)
      ORDER BY set_id NULLS LAST, challenge_code
      LIMIT $3 OFFSET $4
    """
    rows = await pg.fetch(sql, set_id, code, limit, offset)
    return {"ok": True, "data": [dict(r) for r in rows]}