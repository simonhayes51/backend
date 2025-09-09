# app/routers/auth_me.py
from fastapi import APIRouter, Request
from pydantic import BaseModel
from typing import Any, Dict
from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/api/auth", tags=["auth"])

class MeOut(BaseModel):
  user_id: str | None
  username: str | None
  plan: str | None
  is_premium: bool
  features: list[str]
  limits: Dict[str, Any]

@router.get("/me", response_model=MeOut)
async def me(req: Request):
  user = (req.session or {}).get("user") or {}
  ent = await compute_entitlements(req)
  return {
    "user_id": ent["user_id"],
    "username": user.get("username") or user.get("global_name"),
    "plan": ent["plan"],
    "is_premium": ent["is_premium"],
    "features": ent["features"],
    "limits": ent["limits"],
  }
