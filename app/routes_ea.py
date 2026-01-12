from fastapi import APIRouter, Depends, HTTPException
from app.ea_client import ea_get, ExpiredEA, RateLimitedEA
import os

router = APIRouter(prefix="/api/ea", tags=["ea"])

def get_sid_for_user() -> str:
    sid = os.getenv("EA_X_UT_SID")
    if not sid:
        raise HTTPException(status_code=428, detail="ea_session_missing")
    return sid

@router.get("/sbs/sets")
async def ea_sets():
    sid = get_sid_for_user()
    try:
        data = await ea_get("sbs/sets", sid)
        return {"ok": True, "data": data}
    except ExpiredEA:
        raise HTTPException(status_code=401, detail="ea_session_expired")
    except RateLimitedEA as e:
        raise HTTPException(status_code=429, detail={"retry_after": e.retry_after})

@router.get("/sbs/{set_id}/challenges")
async def ea_challenges(set_id: int):
    sid = get_sid_for_user()
    try:
        data = await ea_get(f"sbs/setId/{set_id}/challenges", sid)
        return {"ok": True, "data": data}
    except ExpiredEA:
        raise HTTPException(status_code=401, detail="ea_session_expired")
    except RateLimitedEA as e:
        raise HTTPException(status_code=429, detail={"retry_after": e.retry_after})
