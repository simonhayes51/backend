# app/routers/leaderboard.py
from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Dict, Any, List, Literal
from app.db import get_db
from app.auth.entitlements import compute_entitlements
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/leaderboard", tags=["Leaderboard"])

@router.get("/{period}")
async def get_leaderboard(
    period: Literal["weekly", "monthly", "alltime"],
    req: Request,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    Rankings by profit with ROI and win rate.
    Support privacy settings to hide users.
    Return user's rank even if outside top 100.
    """
    try:
        ent = await compute_entitlements(req)
        current_user_id = ent.get("user_id")

        # Determine time cutoff
        now = datetime.utcnow()
        if period == "weekly":
            cutoff = now - timedelta(days=7)
        elif period == "monthly":
            cutoff = now - timedelta(days=30)
        else:  # alltime
            cutoff = datetime(2000, 1, 1)  # Effectively no cutoff

        # Get leaderboard data
        # Note: We'll need to add a privacy setting later, for now everyone is visible
        leaderboard_query = """
            WITH user_stats AS (
                SELECT
                    user_id,
                    COUNT(*) as total_trades,
                    SUM(profit) as total_profit,
                    SUM(buy * quantity) as total_invested,
                    SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)::FLOAT / COUNT(*)::FLOAT as win_rate
                FROM trades
                WHERE timestamp >= $1
                GROUP BY user_id
                HAVING COUNT(*) >= 5  -- Minimum 5 trades to appear
            ),
            ranked AS (
                SELECT
                    user_id,
                    total_trades,
                    total_profit,
                    CASE
                        WHEN total_invested > 0 THEN (total_profit::FLOAT / total_invested::FLOAT) * 100
                        ELSE 0
                    END as roi,
                    win_rate,
                    ROW_NUMBER() OVER (ORDER BY total_profit DESC) as rank
                FROM user_stats
            )
            SELECT * FROM ranked
            ORDER BY rank
            LIMIT 100
        """

        leaderboard = await db.fetch(leaderboard_query, cutoff)

        # Format leaderboard
        top_100 = []
        user_found_in_top_100 = False

        for row in leaderboard:
            # Check if this is the current user
            is_current = current_user_id and row["user_id"] == current_user_id
            if is_current:
                user_found_in_top_100 = True

            # Privacy: In production, check user settings
            # For now, show anonymized usernames
            username = row["user_id"][:8] + "..." if len(row["user_id"]) > 8 else row["user_id"]

            if is_current:
                username = "You"  # Always show current user

            top_100.append({
                "rank": row["rank"],
                "user_id": row["user_id"] if is_current else None,  # Only reveal if current user
                "username": username,
                "total_profit": int(row["total_profit"]),
                "total_trades": row["total_trades"],
                "roi": round(row["roi"], 2),
                "win_rate": round((row["win_rate"] or 0) * 100, 1),
                "is_you": is_current
            })

        # If current user not in top 100, find their rank
        current_user_rank = None
        if current_user_id and not user_found_in_top_100:
            user_rank_query = """
                WITH user_stats AS (
                    SELECT
                        user_id,
                        COUNT(*) as total_trades,
                        SUM(profit) as total_profit,
                        SUM(buy * quantity) as total_invested,
                        SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END)::FLOAT / COUNT(*)::FLOAT as win_rate
                    FROM trades
                    WHERE timestamp >= $1
                    GROUP BY user_id
                    HAVING COUNT(*) >= 5
                ),
                ranked AS (
                    SELECT
                        user_id,
                        total_trades,
                        total_profit,
                        CASE
                            WHEN total_invested > 0 THEN (total_profit::FLOAT / total_invested::FLOAT) * 100
                            ELSE 0
                        END as roi,
                        win_rate,
                        ROW_NUMBER() OVER (ORDER BY total_profit DESC) as rank
                    FROM user_stats
                )
                SELECT * FROM ranked
                WHERE user_id = $2
            """

            user_row = await db.fetchrow(user_rank_query, cutoff, current_user_id)
            if user_row:
                current_user_rank = {
                    "rank": user_row["rank"],
                    "username": "You",
                    "total_profit": int(user_row["total_profit"]),
                    "total_trades": user_row["total_trades"],
                    "roi": round(user_row["roi"], 2),
                    "win_rate": round((user_row["win_rate"] or 0) * 100, 1),
                    "is_you": True
                }

        return {
            "ok": True,
            "period": period,
            "leaderboard": top_100,
            "your_rank": current_user_rank,
            "total_entries": len(leaderboard),
            "timestamp": now.isoformat()
        }

    except Exception as e:
        import logging
        logging.error(f"leaderboard error: {e}")
        raise HTTPException(status_code=500, detail=f"Leaderboard failed: {str(e)}")
