from fastapi import APIRouter, Depends, HTTPException, Request
import asyncpg
from app.db import get_db

router = APIRouter(prefix="/api/admin", tags=["Admin"])


def get_admin_user(request: Request) -> dict:
  session = request.session or {}
  user = session.get("user") or {}
  roles = session.get("roles") or []

  is_admin = False
  if user.get("role") == "admin" or user.get("is_admin"):
    is_admin = True
  else:
    role_names = [str(r).lower() for r in roles]
    if any("admin" in name for name in role_names):
      is_admin = True

  if not user or not is_admin:
    raise HTTPException(status_code=403, detail="Admin access required")

  if is_admin and not user.get("role"):
    user["role"] = "admin"
    session["user"] = user

  return user


@router.get("/ping")
async def ping_admin():
  return {"message": "Admin router is working"}

@router.get("/pending-traders")
async def get_pending_traders(
  request: Request,
  db: asyncpg.Connection = Depends(get_db),
):
  get_admin_user(request)
  rows = await db.fetch(
    """
    SELECT tp.*, up.username, up.avatar_url
    FROM trader_profiles tp
    JOIN user_profiles up ON tp.user_id = up.user_id
    WHERE tp.verified = FALSE
    ORDER BY tp.created_at DESC
    """
  )
  return [dict(row) for row in rows]


@router.post("/pending-traders/{trader_id}/approve")
async def approve_trader(
  trader_id: str,
  request: Request,
  db: asyncpg.Connection = Depends(get_db),
):
  get_admin_user(request)
  result = await db.execute(
    """
    UPDATE trader_profiles
    SET verified = TRUE
    WHERE user_id = $1
    """,
    trader_id,
  )
  
  # Also update the user's account_type to 'trader'
  await db.execute(
    """
    UPDATE users
    SET account_type = 'trader'
    WHERE id = $1
    """,
    trader_id
  )

  if result == "UPDATE 0":
    raise HTTPException(status_code=404, detail="Trader profile not found")
  return {"success": True}


@router.post("/pending-traders/{trader_id}/reject")
async def reject_trader(
  trader_id: str,
  request: Request,
  db: asyncpg.Connection = Depends(get_db),
):
  get_admin_user(request)
  result = await db.execute(
    """
    DELETE FROM trader_profiles
    WHERE user_id = $1 AND verified = FALSE
    """,
    trader_id,
  )
  if result == "DELETE 0":
    raise HTTPException(status_code=404, detail="Trader profile not found or already verified")
  return {"success": True}
