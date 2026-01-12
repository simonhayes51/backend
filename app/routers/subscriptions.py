"""
Trader Subscriptions Router - Follow and subscribe to traders
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List
import asyncpg
from asyncpg import exceptions as asyncpg_exceptions

from app.models.social import (
    SubscriptionCreate,
    Subscription,
    SubscriptionWithTrader,
)
from app.db import get_pool

router = APIRouter(prefix="/api/subscriptions", tags=["Subscriptions"])
social_router = APIRouter(prefix="/api/social/subscriptions", tags=["Subscriptions"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def get_db():
    """Database connection dependency"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        yield conn


async def table_exists(db: asyncpg.Connection, table_name: str) -> bool:
    return await db.fetchval("SELECT to_regclass($1)", table_name) is not None


async def ensure_tables_exist(db: asyncpg.Connection, table_names: List[str]) -> bool:
    for table_name in table_names:
        if not await table_exists(db, table_name):
            return False
    return True


@router.post("/subscribe", response_model=Subscription)
async def subscribe_to_trader(
    subscription: SubscriptionCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Subscribe/follow a trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Can't subscribe to yourself
    if user_id == subscription.trader_id:
        raise HTTPException(status_code=400, detail="Cannot subscribe to yourself")

    # Check if trader exists and is actually a trader
    trader = await db.fetchrow(
        "SELECT account_type FROM users WHERE id = $1",
        subscription.trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    if trader["account_type"] != "trader":
        raise HTTPException(status_code=400, detail="User is not a trader")

    # Check if already subscribed
    existing = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND trader_id = $2
        """,
        user_id,
        subscription.trader_id
    )

    if existing:
        # Reactivate if was unsubscribed
        if not existing["is_active"]:
            row = await db.fetchrow(
                """
                UPDATE trader_subscriptions
                SET is_active = TRUE, unsubscribed_at = NULL
                WHERE id = $1
                RETURNING *
                """,
                existing["id"]
            )
            return dict(row)
        else:
            raise HTTPException(status_code=400, detail="Already subscribed to this trader")

    # Create new subscription (free by default)
    query = """
        INSERT INTO trader_subscriptions (
            subscriber_id, trader_id, is_active, subscription_type
        )
        VALUES ($1, $2, TRUE, 'free')
        RETURNING *
    """

    row = await db.fetchrow(query, user_id, subscription.trader_id)

    # Create notification for the trader
    await db.execute(
        """
        INSERT INTO notifications (
            user_id, notification_type, title, message, related_user_id
        )
        VALUES ($1, 'new_follower', 'New Follower', $2, $3)
        """,
        subscription.trader_id,
        f"You have a new follower!",
        user_id
    )

    return dict(row)


@router.delete("/unsubscribe/{trader_id}")
async def unsubscribe_from_trader(
    trader_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Unsubscribe/unfollow a trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Update subscription to inactive
    result = await db.fetchrow(
        """
        UPDATE trader_subscriptions
        SET is_active = FALSE, unsubscribed_at = NOW()
        WHERE subscriber_id = $1 AND trader_id = $2
        RETURNING *
        """,
        user_id,
        trader_id
    )

    if not result:
        raise HTTPException(status_code=404, detail="Subscription not found")

    return {"message": "Successfully unsubscribed"}


@router.get("/my-subscriptions", response_model=List[SubscriptionWithTrader])
async def get_my_subscriptions(
    request: Request,
    active_only: bool = Query(True),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get list of traders the current user is subscribed to
    """
    user = get_current_user(request)
    user_id = user["id"]

    where_clause = "ts.subscriber_id = $1"
    params = [user_id]

    if active_only:
        where_clause += " AND ts.is_active = TRUE"

    query = f"""
        SELECT
            ts.id,
            ts.trader_id,
            up.username as trader_username,
            up.avatar_url as trader_avatar,
            COALESCE(tp.verified, FALSE) as verified,
            ts.is_active,
            ts.subscription_type,
            ts.subscribed_at
        FROM trader_subscriptions ts
        JOIN user_profiles up ON ts.trader_id = up.user_id
        LEFT JOIN trader_profiles tp ON ts.trader_id = tp.user_id
        WHERE {where_clause}
        ORDER BY ts.subscribed_at DESC
    """

    rows = await db.fetch(query, *params)
    return [dict(row) for row in rows]


@router.get("/followers", response_model=List[dict])
async def get_my_followers(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get list of users following the current trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is a trader
    account_type = await db.fetchval(
        "SELECT account_type FROM users WHERE id = $1",
        user_id
    )

    if account_type != "trader":
        raise HTTPException(status_code=403, detail="Only traders can view followers")

    query = """
        SELECT
            ts.subscriber_id as user_id,
            up.username,
            up.avatar_url,
            ts.subscribed_at,
            ts.subscription_type
        FROM trader_subscriptions ts
        JOIN user_profiles up ON ts.subscriber_id = up.user_id
        WHERE ts.trader_id = $1 AND ts.is_active = TRUE
        ORDER BY ts.subscribed_at DESC
    """

    rows = await db.fetch(query, user_id)
    return [dict(row) for row in rows]


@router.get("/check/{trader_id}")
async def check_subscription(
    trader_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Check if current user is subscribed to a trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    subscription = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
        """,
        user_id,
        trader_id
    )

    return {
        "is_subscribed": subscription is not None,
        "subscription": dict(subscription) if subscription else None
    }


@router.get("/stats")
async def get_subscription_stats(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get subscription statistics for the current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Get following count
    following_count = await db.fetchval(
        """
        SELECT COUNT(*)
        FROM trader_subscriptions
        WHERE subscriber_id = $1 AND is_active = TRUE
        """,
        user_id
    )

    # Get followers count (if trader)
    followers_count = await db.fetchval(
        """
        SELECT COUNT(*)
        FROM trader_subscriptions
        WHERE trader_id = $1 AND is_active = TRUE
        """,
        user_id
    )

    account_type = await db.fetchval(
        "SELECT account_type FROM users WHERE id = $1",
        user_id
    )

    return {
        "account_type": account_type,
        "following_count": following_count,
        "followers_count": followers_count or 0
    }


@router.get("/recommended-traders")
async def get_recommended_traders(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get recommended traders to follow based on ratings and followers
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        is_authenticated = True
    except HTTPException:
        user_id = None
        is_authenticated = False

    required_tables = [
        "public.users",
        "public.user_profiles",
        "public.trader_profiles",
    ]
    if not await ensure_tables_exist(db, required_tables):
        return []

    if is_authenticated and not await table_exists(db, "public.trader_subscriptions"):
        is_authenticated = False
        user_id = None

    # Get traders user is not following yet (or all if not authenticated)
    if is_authenticated:
        query = """
            SELECT
                u.id,
                up.username,
                up.avatar_url,
                tp.bio,
                tp.specialties,
                tp.verified,
                tp.total_followers,
                tp.total_posts,
                tp.avg_rating,
                tp.total_ratings,
                FALSE as is_subscribed
            FROM users u
            JOIN user_profiles up ON u.id = up.user_id
            JOIN trader_profiles tp ON u.id = tp.user_id
            WHERE u.account_type = 'trader'
                AND u.id != $1
                AND u.id NOT IN (
                    SELECT trader_id FROM trader_subscriptions
                    WHERE subscriber_id = $1 AND is_active = TRUE
                )
            ORDER BY
                tp.verified DESC,
                tp.avg_rating DESC,
                tp.total_followers DESC,
                tp.total_posts DESC
            LIMIT $2
        """
        try:
            rows = await db.fetch(query, user_id, limit)
        except asyncpg_exceptions.UndefinedTableError:
            return []
    else:
        query = """
            SELECT
                u.id,
                up.username,
                up.avatar_url,
                tp.bio,
                tp.specialties,
                tp.verified,
                tp.total_followers,
                tp.total_posts,
                tp.avg_rating,
                tp.total_ratings,
                FALSE as is_subscribed
            FROM users u
            JOIN user_profiles up ON u.id = up.user_id
            JOIN trader_profiles tp ON u.id = tp.user_id
            WHERE u.account_type = 'trader'
            ORDER BY
                tp.verified DESC,
                tp.avg_rating DESC,
                tp.total_followers DESC,
                tp.total_posts DESC
            LIMIT $1
        """
        try:
            rows = await db.fetch(query, limit)
        except asyncpg_exceptions.UndefinedTableError:
            return []

    return [dict(row) for row in rows]


@router.get("/recommended")
async def get_recommended_traders_alias(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Alias: Get recommended traders to follow
    """
    return await get_recommended_traders(request=request, limit=limit, db=db)


@social_router.get("/recommended")
async def get_social_recommended_traders(
    request: Request,
    limit: int = Query(10, ge=1, le=50),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Social alias: Get recommended traders to follow
    """
    return await get_recommended_traders(request=request, limit=limit, db=db)
