# app/routers/referrals.py
from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Dict, Any, List
from pydantic import BaseModel
from app.db import get_db
from app.auth.entitlements import compute_entitlements
import secrets
import string
from datetime import datetime

router = APIRouter(prefix="/api/referrals", tags=["Referrals"])

def generate_referral_code(user_id: str) -> str:
    """Generate a unique referral code for a user"""
    # Use first 4 chars of user_id + random string
    prefix = user_id[:4].upper() if len(user_id) >= 4 else user_id.upper()
    random_part = ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(6))
    return f"{prefix}{random_part}"

@router.get("/stats")
async def get_referral_stats(
    req: Request,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    Generate unique referral codes, track clicks and conversions,
    calculate rewards earned.
    """
    try:
        ent = await compute_entitlements(req)
        user_id = ent.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")

        # Check if user already has a referral code
        existing_code = await db.fetchrow(
            "SELECT code, created_at FROM referral_codes WHERE user_id = $1",
            user_id
        )

        referral_code = None
        if existing_code:
            referral_code = existing_code["code"]
        else:
            # Generate new code
            referral_code = generate_referral_code(user_id)

            # Insert into database
            try:
                await db.execute(
                    """
                    INSERT INTO referral_codes (user_id, code, created_at)
                    VALUES ($1, $2, $3)
                    ON CONFLICT (user_id) DO NOTHING
                    """,
                    user_id, referral_code, datetime.utcnow()
                )
            except Exception:
                # If conflict, fetch the existing code
                existing = await db.fetchrow(
                    "SELECT code FROM referral_codes WHERE user_id = $1",
                    user_id
                )
                if existing:
                    referral_code = existing["code"]

        # Get click stats
        clicks = await db.fetchval(
            """
            SELECT COUNT(*)
            FROM referral_clicks
            WHERE code = $1
            """,
            referral_code
        ) or 0

        unique_clicks = await db.fetchval(
            """
            SELECT COUNT(DISTINCT ip_address)
            FROM referral_clicks
            WHERE code = $1
            """,
            referral_code
        ) or 0

        # Get conversion stats (users who signed up via this code)
        conversions = await db.fetchval(
            """
            SELECT COUNT(*)
            FROM users
            WHERE referred_by = $1
            """,
            referral_code
        ) or 0

        # Calculate rewards
        # Reward structure: 100 coins per conversion (can be adjusted)
        reward_per_conversion = 100
        total_rewards = conversions * reward_per_conversion

        # Get recent referrals
        recent_referrals = await db.fetch(
            """
            SELECT id, created_at
            FROM users
            WHERE referred_by = $1
            ORDER BY created_at DESC
            LIMIT 10
            """,
            referral_code
        )

        referral_list = [
            {
                "user_id": r["id"][:8] + "...",  # Anonymized
                "joined_at": r["created_at"].isoformat() if r["created_at"] else None
            }
            for r in recent_referrals
        ]

        return {
            "ok": True,
            "referral_code": referral_code,
            "stats": {
                "total_clicks": clicks,
                "unique_clicks": unique_clicks,
                "conversions": conversions,
                "conversion_rate": round((conversions / unique_clicks * 100) if unique_clicks > 0 else 0, 2)
            },
            "rewards": {
                "total_earned": total_rewards,
                "per_conversion": reward_per_conversion,
                "currency": "coins"
            },
            "recent_referrals": referral_list,
            "share_url": f"https://app.futhub.co.uk?ref={referral_code}"
        }

    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"referral-stats error: {e}")
        raise HTTPException(status_code=500, detail=f"Referral stats failed: {str(e)}")

@router.post("/track-click")
async def track_referral_click(
    code: str,
    req: Request,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """Track a referral click"""
    try:
        # Get IP address from request
        ip_address = req.client.host if req.client else "unknown"
        user_agent = req.headers.get("user-agent", "")

        # Insert click record
        await db.execute(
            """
            INSERT INTO referral_clicks (code, ip_address, user_agent, clicked_at)
            VALUES ($1, $2, $3, $4)
            """,
            code, ip_address, user_agent, datetime.utcnow()
        )

        return {"ok": True, "message": "Click tracked"}

    except Exception as e:
        import logging
        logging.error(f"track-click error: {e}")
        # Don't fail hard on tracking errors
        return {"ok": False, "message": "Failed to track click"}
