from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional

from app.db import get_db  # <-- returns an asyncpg.Connection
from app.routes_ea import get_sid_for_user
from app.ea_client import ea_get, ExpiredEA, RateLimitedEA
from app.ea_sbc_sets_ingest import upsert_sets_payload
from app.ea_sbc_ingest import upsert_set_challenges

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

@router.post("/sbs/sets")
async def ingest_sets(
    include_challenges: bool = Query(True, description="Also fetch & upsert challenges for each set"),
    pg = Depends(get_db),  # asyncpg.Connection (already acquired)
):
    """
    Pulls /sbs/sets from EA, upserts categories & sets to Postgres,
    and (optionally) fetches challenges per set and upserts them too.
    """
    sid = get_sid_for_user()  # reads from env or your user-store

    try:
        sets_payload = await ea_get("sbs/sets", sid)
    except ExpiredEA:
        raise HTTPException(status_code=401, detail="ea_session_expired")
    except RateLimitedEA as e:
        raise HTTPException(status_code=429, detail={"retry_after": e.retry_after})

    # Upsert categories + sets
    count_sets = await upsert_sets_payload(pg, sets_payload)

    # Optionally walk each set and ingest challenges
    count_chals = 0
    if include_challenges:
        for cat in sets_payload.get("categories") or []:
            for s in cat.get("sets") or []:
                set_id = s.get("setId")
                if not set_id:
                    continue
                try:
                    ch_payload = await ea_get(f"sbs/setId/{set_id}/challenges", sid)
                except ExpiredEA:
                    raise HTTPException(status_code=401, detail="ea_session_expired")
                except RateLimitedEA as e:
                    # partial success; return what we have with a retry hint
                    return {
                        "ok": True,
                        "sets": count_sets,
                        "challenges": count_chals,
                        "note": "Rate limited while fetching challenges.",
                        "retry_after": e.retry_after,
                    }
                count_chals += await upsert_set_challenges(pg, int(set_id), ch_payload)

    return {"ok": True, "sets": count_sets, "challenges": count_chals}