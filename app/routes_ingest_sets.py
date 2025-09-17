from fastapi import APIRouter, Depends, HTTPException
from app.db import get_pg
from app.routes_ea import get_sid_for_user
from app.ea_client import ea_get, ExpiredEA, RateLimitedEA
from app.ea_sbc_sets_ingest import upsert_sets_payload
from app.ea_sbc_ingest import upsert_set_challenges

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

@router.post("/sbs/sets")
async def ingest_sets(include_challenges: bool = True, pg=Depends(get_pg)):
    sid = get_sid_for_user()
    try:
        sets_payload = await ea_get("sbs/sets", sid)
    except ExpiredEA:
        raise HTTPException(status_code=401, detail="ea_session_expired")
    except RateLimitedEA as e:
        raise HTTPException(status_code=429, detail={"retry_after": e.retry_after})

    async with pg.acquire() as con:
        count_sets = await upsert_sets_payload(con, sets_payload)
        count_chals = 0
        if include_challenges:
            for cat in sets_payload.get("categories") or []:
                for s in cat.get("sets") or []:
                    set_id = s.get("setId")
                    if not set_id:
                        continue
                    ch_payload = await ea_get(f"sbs/setId/{set_id}/challenges", sid)
                    count_chals += await upsert_set_challenges(con, int(set_id), ch_payload)

    return {"ok": True, "sets": count_sets, "challenges": count_chals}
