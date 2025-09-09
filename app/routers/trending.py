# app/routers/trending.py
from fastapi import APIRouter, Request, HTTPException, Query
from pydantic import BaseModel
from typing import Literal, List
from app.auth.entitlements import compute_entitlements, require_feature

router = APIRouter(prefix="/api/trending", tags=["trending"])

class TrendingOut(BaseModel):
  type: Literal["risers", "fallers", "smart"]
  timeframe: Literal["4h", "6h", "24h"]
  items: List[dict]
  limited: bool = False  # true if free-user constraints applied

@router.get("", response_model=TrendingOut)
async def trending(
  req: Request,
  type: Literal["risers","fallers","smart"] = Query("risers"),
  timeframe: Literal["4h","6h","24h"] = Query("24h"),
  limit: int = Query(10, ge=1, le=50)
):
  ent = await compute_entitlements(req)
  limits = ent["limits"]["trending"]
  limited = False

  # Smart tab requires premium
  if type == "smart" and not ent["is_premium"]:
    raise HTTPException(
      status_code=402,
      detail={
        "error":"payment_required",
        "feature":"smart_trending",
        "message":"Smart Trending is a premium feature.",
        "upgrade_url":"/billing",
      }
    )

  # Coerce timeframe for free users
  if timeframe not in limits["timeframes"]:
    timeframe = "24h"
    limited = True

  # Cap result size
  if limit > limits["limit"]:
    limit = limits["limit"]
    limited = True

  # ...fetch & build your items here using (type, timeframe, limit)
  items = await fetch_trending_items(type=type, timeframe=timeframe, limit=limit)

  return {"type": type, "timeframe": timeframe, "items": items, "limited": limited}
