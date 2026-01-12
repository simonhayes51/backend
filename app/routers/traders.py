"""
Traders Router - Discover, browse, and manage trader profiles
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List, Optional
from pydantic import BaseModel
import asyncpg

from app.models.social import (
    TraderProfileCreate,
    TraderProfileUpdate,
    TraderProfile,
    TraderPublicProfile,
    TraderStats,
)
from app.db import get_pool

router = APIRouter(prefix="/api/traders", tags=["Traders"])
admin_router = APIRouter(prefix="/api/admin/traders", tags=["Traders"])
social_router = APIRouter(prefix="/api/social/traders", tags=["Traders"])


def get_current_user(request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def get_db():
    """Database connection dependency"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


class TraderAssignRequest(BaseModel):
    user_id: int
    profile: Optional[TraderProfileCreate] = None


async def grant_trader_role(
    db: asyncpg.Connection,
    user_id: int,
    profile: Optional[TraderProfileCreate] = None
) -> dict:
    profile = profile or TraderProfileCreate()

    existing_user = await db.fetchrow(
        "SELECT id, account_type FROM users WHERE id = $1",
        user_id
    )
    if not existing_user:
        raise HTTPException(status_code=404, detail="User not found")

    async with db.transaction():
        await db.execute(
            "UPDATE users SET account_type = 'trader' WHERE id = $1",
            user_id
        )
        await db.execute(
            """
            INSERT INTO trader_profiles (user_id, bio, specialties, subscription_price)
            VALUES ($1, $2, $3, $4)
            ON CONFLICT (user_id)
            DO UPDATE SET
                bio = EXCLUDED.bio,
                specialties = EXCLUDED.specialties,
                subscription_price = EXCLUDED.subscription_price,
                updated_at = NOW()
            """,
            user_id,
            profile.bio,
            profile.specialties or [],
            profile.subscription_price or 0
        )

    return {"message": "Trader role granted", "user_id": user_id}


@router.post("/upgrade-to-trader")
async def upgrade_to_trader(
    profile: TraderProfileCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Upgrade user account to trader account
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check current account type
    account_type = await db.fetchval(
        "SELECT account_type FROM users WHERE id = $1",
        user_id
    )

    if account_type == "trader":
        raise HTTPException(status_code=400, detail="Already a trader account")

    # Start transaction
    async with db.transaction():
        # Update user account type
        await db.execute(
            "UPDATE users SET account_type = 'trader' WHERE id = $1",
            user_id
        )

        # Create trader profile
        row = await db.fetchrow(
            """
            INSERT INTO trader_profiles (
                user_id, bio, specialties, subscription_price
            )
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            user_id,
            profile.bio,
            profile.specialties or [],
            profile.subscription_price or 0
        )

    return {
        "message": "Successfully upgraded to trader account",
        "profile": dict(row)
    }


@router.post("/upgrade")
async def upgrade_to_trader_alias(
    profile: TraderProfileCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Upgrade user account to trader account
    """
    return await upgrade_to_trader(profile=profile, request=request, db=db)


@router.post("/request")
async def request_trader_upgrade(
    profile: TraderProfileCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Request trader upgrade
    """
    return await upgrade_to_trader(profile=profile, request=request, db=db)


@router.post("/apply")
async def apply_for_trader_upgrade(
    profile: TraderProfileCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Apply for trader upgrade
    """
    return await upgrade_to_trader(profile=profile, request=request, db=db)


@router.get("/requests")
async def list_trader_requests(
    db: asyncpg.Connection = Depends(get_db)
):
    """
    List pending trader requests (placeholder)
    """
    return []


@router.post("/requests/{request_id}/approve")
async def approve_trader_request(
    request_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Approve a trader request by user id
    """
    return await grant_trader_role(db=db, user_id=request_id)


@router.post("/requests/{request_id}/reject")
async def reject_trader_request(
    request_id: int
):
    """
    Reject a trader request by user id
    """
    return {"message": "Trader request rejected", "user_id": request_id}


@router.post("/assign")
async def assign_trader_role(
    payload: TraderAssignRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Assign trader role to a user
    """
    return await grant_trader_role(
        db=db,
        user_id=payload.user_id,
        profile=payload.profile
    )


@router.patch("/profile")
async def update_trader_profile(
    profile_update: TraderProfileUpdate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Update trader profile (traders only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is a trader
    account_type = await db.fetchval(
        "SELECT account_type FROM users WHERE id = $1",
        user_id
    )

    if account_type != "trader":
        raise HTTPException(status_code=403, detail="Only traders can update trader profile")

    # Build update query
    updates = []
    params = []
    param_idx = 1

    if profile_update.bio is not None:
        updates.append(f"bio = ${param_idx}")
        params.append(profile_update.bio)
        param_idx += 1

    if profile_update.specialties is not None:
        updates.append(f"specialties = ${param_idx}")
        params.append(profile_update.specialties)
        param_idx += 1

    if profile_update.subscription_price is not None:
        updates.append(f"subscription_price = ${param_idx}")
        params.append(profile_update.subscription_price)
        param_idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    updates.append("updated_at = NOW()")
    params.append(user_id)

    query = f"""
        UPDATE trader_profiles
        SET {', '.join(updates)}
        WHERE user_id = ${param_idx}
        RETURNING *
    """

    row = await db.fetchrow(query, *params)

    if not row:
        raise HTTPException(status_code=404, detail="Trader profile not found")

    return dict(row)


@router.get("/profile/{trader_id}", response_model=TraderPublicProfile)
async def get_trader_profile(
    trader_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get public profile for a trader
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        is_authenticated = True
    except HTTPException:
        user_id = None
        is_authenticated = False

    query = """
        SELECT
            u.id,
            up.username,
            up.avatar_url,
            tp.bio,
            tp.specialties,
            tp.verified,
            tp.subscription_price,
            tp.total_followers,
            tp.total_posts,
            tp.avg_rating,
            tp.total_ratings,
            tp.created_at as trader_since
        FROM users u
        JOIN user_profiles up ON u.id = up.user_id
        JOIN trader_profiles tp ON u.id = tp.user_id
        WHERE u.id = $1 AND u.account_type = 'trader'
    """

    row = await db.fetchrow(query, trader_id)

    if not row:
        raise HTTPException(status_code=404, detail="Trader not found")

    trader_dict = dict(row)

    # Check if current user is subscribed
    if is_authenticated:
        is_subscribed = await db.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM trader_subscriptions
                WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
            )
            """,
            user_id,
            trader_id
        )
        trader_dict["is_subscribed"] = is_subscribed
    else:
        trader_dict["is_subscribed"] = False

    return trader_dict


@router.get("/browse")
async def browse_traders(
    request: Request,
    search: Optional[str] = Query(None, description="Search by username or bio"),
    specialty: Optional[str] = Query(None, description="Filter by specialty"),
    verified_only: bool = Query(False),
    sort_by: str = Query("followers", description="followers, rating, posts, recent"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Browse and search traders with filters and sorting
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        is_authenticated = True
    except HTTPException:
        user_id = None
        is_authenticated = False

    # Build query conditions
    conditions = ["u.account_type = 'trader'"]
    params = []
    param_idx = 1

    if search:
        conditions.append(f"(up.username ILIKE ${param_idx} OR tp.bio ILIKE ${param_idx})")
        params.append(f"%{search}%")
        param_idx += 1

    if specialty:
        conditions.append(f"${param_idx} = ANY(tp.specialties)")
        params.append(specialty)
        param_idx += 1

    if verified_only:
        conditions.append("tp.verified = TRUE")

    where_clause = " AND ".join(conditions)

    # Determine sort order
    sort_options = {
        "followers": "tp.total_followers DESC",
        "rating": "tp.avg_rating DESC, tp.total_ratings DESC",
        "posts": "tp.total_posts DESC",
        "recent": "tp.created_at DESC"
    }

    sort_clause = sort_options.get(sort_by, "tp.total_followers DESC")

    # Get total count
    count_query = f"""
        SELECT COUNT(*)
        FROM users u
        JOIN trader_profiles tp ON u.id = tp.user_id
        JOIN user_profiles up ON u.id = up.user_id
        WHERE {where_clause}
    """

    total = await db.fetchval(count_query, *params)

    # Get traders
    params.extend([limit, offset])
    query = f"""
        SELECT
            u.id,
            up.username,
            up.avatar_url,
            tp.bio,
            tp.specialties,
            tp.verified,
            tp.subscription_price,
            tp.total_followers,
            tp.total_posts,
            tp.avg_rating,
            tp.total_ratings,
            tp.created_at as trader_since
        FROM users u
        JOIN user_profiles up ON u.id = up.user_id
        JOIN trader_profiles tp ON u.id = tp.user_id
        WHERE {where_clause}
        ORDER BY {sort_clause}
        LIMIT ${param_idx} OFFSET ${param_idx + 1}
    """

    rows = await db.fetch(query, *params)

    traders = []
    for row in rows:
        trader_dict = dict(row)

        # Check if current user is subscribed
        if is_authenticated:
            is_subscribed = await db.fetchval(
                """
                SELECT EXISTS(
                    SELECT 1 FROM trader_subscriptions
                    WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
                )
                """,
                user_id,
                trader_dict["id"]
            )
            trader_dict["is_subscribed"] = is_subscribed
        else:
            trader_dict["is_subscribed"] = False

        traders.append(trader_dict)

    return {
        "traders": traders,
        "total": total,
        "has_more": offset + limit < total,
        "offset": offset,
        "limit": limit
    }


@router.get("/stats/{trader_id}", response_model=TraderStats)
async def get_trader_stats(
    trader_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get detailed statistics for a trader
    """
    # Check if trader exists
    trader = await db.fetchrow(
        """
        SELECT u.account_type, tp.*
        FROM users u
        LEFT JOIN trader_profiles tp ON u.id = tp.user_id
        WHERE u.id = $1
        """,
        trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="User not found")

    # Get post count
    total_posts = await db.fetchval(
        "SELECT COUNT(*) FROM social_posts WHERE user_id = $1",
        trader_id
    )

    # Get total following count
    total_following = await db.fetchval(
        """
        SELECT COUNT(*)
        FROM trader_subscriptions
        WHERE subscriber_id = $1 AND is_active = TRUE
        """,
        trader_id
    )

    # Get engagement stats
    engagement = await db.fetchrow(
        """
        SELECT
            COALESCE(SUM(likes_count), 0) as total_likes_received,
            COALESCE(SUM(comments_count), 0) as total_comments_received
        FROM social_posts
        WHERE user_id = $1
        """,
        trader_id
    )

    stats = {
        "account_type": trader["account_type"],
        "total_posts": total_posts,
        "total_followers": trader.get("total_followers", 0),
        "total_following": total_following,
        "total_likes_received": engagement["total_likes_received"],
        "total_comments_received": engagement["total_comments_received"],
    }

    # Add trader-specific stats if applicable
    if trader["account_type"] == "trader":
        stats.update({
            "avg_rating": trader["avg_rating"],
            "total_ratings": trader["total_ratings"],
            "verified": trader["verified"],
            "subscription_price": trader["subscription_price"]
        })

    return stats


@router.get("/my-profile")
async def get_my_profile(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get current user's trader profile (if trader)
    """
    user = get_current_user(request)
    user_id = user["id"]

    account_type = await db.fetchval(
        "SELECT account_type FROM users WHERE id = $1",
        user_id
    )

    if account_type != "trader":
        return {
            "account_type": account_type,
            "is_trader": False,
            "message": "Not a trader account"
        }

    profile = await db.fetchrow(
        """
        SELECT tp.*, up.username, up.avatar_url
        FROM trader_profiles tp
        JOIN user_profiles up ON tp.user_id = up.user_id
        WHERE tp.user_id = $1
        """,
        user_id
    )

    return {
        "account_type": account_type,
        "is_trader": True,
        "profile": dict(profile) if profile else None
    }


@router.get("/specialties")
async def get_available_specialties(
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get list of all available trader specialties
    """
    # Get unique specialties from all traders
    rows = await db.fetch(
        """
        SELECT DISTINCT UNNEST(specialties) as specialty
        FROM trader_profiles
        WHERE specialties IS NOT NULL AND ARRAY_LENGTH(specialties, 1) > 0
        ORDER BY specialty
        """
    )

    specialties = [row["specialty"] for row in rows]

    # Add common specialties if not present
    common_specialties = [
        "Quick Flips",
        "SBC Investments",
        "Long Term Trading",
        "Market Analysis",
        "Budget Trading",
        "Icon Trading",
        "Mass Bidding"
    ]

    for specialty in common_specialties:
        if specialty not in specialties:
            specialties.append(specialty)

    return {"specialties": sorted(specialties)}


@admin_router.get("/requests")
async def list_trader_requests_admin(
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Admin: List pending trader requests (placeholder)
    """
    return await list_trader_requests(db=db)


@admin_router.post("/requests/{request_id}/approve")
async def approve_trader_request_admin(
    request_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Admin: Approve trader request
    """
    return await grant_trader_role(db=db, user_id=request_id)


@admin_router.post("/requests/{request_id}/reject")
async def reject_trader_request_admin(
    request_id: int
):
    """
    Admin: Reject trader request
    """
    return {"message": "Trader request rejected", "user_id": request_id}


@admin_router.post("/assign")
async def assign_trader_role_admin(
    payload: TraderAssignRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Admin: Assign trader role to a user
    """
    return await grant_trader_role(
        db=db,
        user_id=payload.user_id,
        profile=payload.profile
    )


@social_router.get("/requests")
async def list_trader_requests_social(
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Social: List pending trader requests (placeholder)
    """
    return await list_trader_requests(db=db)


@social_router.post("/requests/{request_id}/approve")
async def approve_trader_request_social(
    request_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Social: Approve trader request
    """
    return await grant_trader_role(db=db, user_id=request_id)


@social_router.post("/requests/{request_id}/reject")
async def reject_trader_request_social(
    request_id: int
):
    """
    Social: Reject trader request
    """
    return {"message": "Trader request rejected", "user_id": request_id}


@social_router.post("/assign")
async def assign_trader_role_social(
    payload: TraderAssignRequest,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Social: Assign trader role to a user
    """
    return await grant_trader_role(
        db=db,
        user_id=payload.user_id,
        profile=payload.profile
    )


@social_router.post("")
async def social_trader_upgrade(
    profile: TraderProfileCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Social: Upgrade user account to trader account
    """
    return await upgrade_to_trader(profile=profile, request=request, db=db)
