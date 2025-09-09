# app/routers/watchlist.py
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
import asyncpg
from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

WATCHLIST_TABLE = "watchlist"  # adjust if your table name differs

class WatchlistCreate(BaseModel):
  card_id: int
  platform: str

@router.get("/usage")
async def usage(req: Request):
  ent = await compute_entitlements(req)
  user = (req.session or {}).get("user") or {}
  user_id = user.get("id")
  if not user_id:
    raise HTTPException(status_code=401, detail="Unauthenticated")
  pool: asyncpg.Pool = req.app.state.pool
  used = await pool.fetchval(f"SELECT COUNT(*) FROM {WATCHLIST_TABLE} WHERE user_id=$1", user_id)
  return {"used": int(used or 0), "max": ent["limits"]["watchlist_max"], "is_premium": ent["is_premium"]}

@router.post("")
async def add_watchlist(item: WatchlistCreate, req: Request):
  ent = await compute_entitlements(req)
  user = (req.session or {}).get("user") or {}
  user_id = user.get("id")
  if not user_id:
    raise HTTPException(status_code=401, detail="Unauthenticated")

  pool: asyncpg.Pool = req.app.state.pool
  used = await pool.fetchval(f"SELECT COUNT(*) FROM {WATCHLIST_TABLE} WHERE user_id=$1", user_id)
  max_allowed = ent["limits"]["watchlist_max"]

  if int(used or 0) >= int(max_allowed):
    # soft gate with 402 so frontend can show upgrade CTA
    raise HTTPException(
      status_code=402,
      detail={
        "error": "limit_reached",
        "feature": "watchlist",
        "message": f"Free plan allows up to {max_allowed} watchlist players.",
        "upgrade_url": "/billing",
      },
    )

  # proceed to insert (prevent duplicates)
  await pool.execute(
    f"""INSERT INTO {WATCHLIST_TABLE}(user_id, card_id, platform, created_at)
        VALUES ($1, $2, $3, NOW())
        ON CONFLICT (user_id, card_id, platform) DO NOTHING""",
    user_id, item.card_id, item.platform.lower()
  )
  return {"ok": True}
