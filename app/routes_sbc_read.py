from fastapi import APIRouter, Depends, Query
from app.db import get_db

router = APIRouter(prefix="/api/sbc", tags=["sbc"])

@router.get("/sets")
async def list_sets(pg=Depends(get_db),
                    q: str | None = Query(None),
                    category_id: int | None = Query(None),
                    limit: int = 50, offset: int = 0):
    sql = """
      SELECT set_id, category_id, name, description, challenges_count,
             hidden, repeatable, repeatability_mode, challenges_completed,
             end_time, release_time, set_image_id
      FROM sbc_sets
      WHERE ($1::text IS NULL OR name ILIKE '%'||$1||'%')
        AND ($2::int  IS NULL OR category_id = $2)
      ORDER BY release_time DESC NULLS LAST, set_id DESC
      LIMIT $3 OFFSET $4
    """
    async with pg.acquire() as con:
      rows = await con.fetch(sql, q, category_id, limit, offset)
    return {"ok": True, "data": [dict(r) for r in rows]}

@router.get("/challenges")
async def list_challenges(pg=Depends(get_db),
                          set_id: int | None = Query(None),
                          code: str | None = Query(None),
                          limit: int = 100, offset: int = 0):
    sql = """
      SELECT challenge_code, set_id, name, positions, min_squad_rating, min_chem,
             min_nations, min_leagues, min_clubs, formation, constraints_json
      FROM sbc_challenges
      WHERE ($1::int  IS NULL OR set_id = $1)
        AND ($2::text IS NULL OR challenge_code = $2)
      ORDER BY set_id, challenge_code
      LIMIT $3 OFFSET $4
    """
    async with pg.acquire() as con:
      rows = await con.fetch(sql, set_id, code, limit, offset)
    return {"ok": True, "data": [dict(r) for r in rows]}
