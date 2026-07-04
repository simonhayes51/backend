"""
Trader Subscriptions Router - Follow and subscribe to traders
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List
import os
import asyncpg
import stripe
from asyncpg import exceptions as asyncpg_exceptions

from app.models.social import (
    SubscriptionCreate,
    Subscription,
    SubscriptionWithTrader,
)
from app.db import get_db

router = APIRouter(prefix="/api/subscriptions", tags=["Subscriptions"])
social_router = APIRouter(prefix="/api/social/subscriptions", tags=["Subscriptions"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def table_exists(db: asyncpg.Connection, table_name: str) -> bool:
    return await db.fetchval("SELECT to_regclass($1)", table_name) is not None


async def ensure_tables_exist(db: asyncpg.Connection, table_names: List[str]) -> bool:
    for table_name in table_names:
        if not await table_exists(db, table_name):
            return False
    return True


@router.get("/my-subscribers")
async def my_subscribers(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get list of users subscribed to me
    """
    user = get_current_user(request)
    user_id = user["id"]
    
    rows = await db.fetch(
        """
        SELECT
          ts.subscriber_id,
          ts.is_active,
          ts.created_at,
          u.username,
          u.avatar_url,
          up.global_name
        FROM trader_subscriptions ts
        LEFT JOIN users u ON u.id = ts.subscriber_id
        LEFT JOIN user_profiles up ON up.user_id = ts.subscriber_id
        WHERE ts.trader_id = $1
        ORDER BY ts.created_at DESC
        """,
        user_id,
    )
    return [dict(r) for r in rows]


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

    required_tables = [
        "public.users",
        "public.trader_subscriptions",
        "public.notifications",
    ]
    if not await ensure_tables_exist(db, required_tables):
        raise HTTPException(
            status_code=503,
            detail="Subscriptions feature not available (database not migrated)",
        )

    # Can't subscribe to yourself
    if user_id == subscription.trader_id:
        raise HTTPException(status_code=400, detail="Cannot subscribe to yourself")

    # Validate tier - Paid tiers must go through billing
    if subscription.tier != 'free':
        raise HTTPException(status_code=400, detail="Paid subscriptions must be processed via payment gateway")

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
        if existing["is_active"]:
             if existing["subscription_type"] != 'free':
                 raise HTTPException(status_code=400, detail="You have an active paid subscription. Please manage it via billing settings.")
             else:
                 raise HTTPException(status_code=400, detail="Already following this trader")
        
        # If inactive, we can reactivate as free
        row = await db.fetchrow(
            """
            UPDATE trader_subscriptions
            SET is_active = TRUE, 
                unsubscribed_at = NULL,
                subscription_type = 'free',
                updated_at = NOW()
            WHERE id = $1
            RETURNING *
            """,
            existing["id"]
        )
        return dict(row)

    # Create new subscription
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


@router.post("/{trader_id}/subscribe", response_model=Subscription)
@social_router.post("/{trader_id}/subscribe", response_model=Subscription)
async def subscribe_to_trader_by_id(
    trader_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    if trader_id in {"undefined", "null", ""}:
        raise HTTPException(status_code=400, detail="Trader id required")

    subscription = SubscriptionCreate(trader_id=trader_id)
    return await subscribe_to_trader(
        subscription=subscription,
        request=request,
        db=db,
    )


@router.delete("/unsubscribe/{trader_id}")
@social_router.delete("/unsubscribe/{trader_id}")
async def unsubscribe_from_trader(
    trader_id: str,
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


@router.post("/{trader_id}/unsubscribe")
@social_router.post("/{trader_id}/unsubscribe")
async def unsubscribe_from_trader_by_id(
    trader_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await unsubscribe_from_trader(
        trader_id=trader_id,
        request=request,
        db=db,
    )


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
            ts.subscribed_at,
            COALESCE(tp.avg_rating, 0)::float8 AS avg_rating,
            COALESCE(tp.total_ratings, 0) AS total_ratings
        FROM trader_subscriptions ts
        JOIN user_profiles up ON ts.trader_id = up.user_id
        LEFT JOIN trader_profiles tp ON ts.trader_id = tp.user_id
        WHERE {where_clause}
        ORDER BY ts.subscribed_at DESC
    """

    rows = await db.fetch(query, *params)
    results = []
    for row in rows:
        trader_dict = dict(row)
        trader_dict["trader_id"] = str(trader_dict["trader_id"])
        trader_dict["user_id"] = str(trader_dict["trader_id"])
        results.append(trader_dict)
    return results


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
    results = []
    for row in rows:
        trader_dict = dict(row)
        trader_dict["trader_id"] = str(trader_dict["id"])
        trader_dict["user_id"] = str(trader_dict["id"])
        results.append(trader_dict)
    return results


@router.get("/check/{trader_id}")
async def check_subscription(
    trader_id: str,
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
@router.get("/my-stats")
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

    results = []
    for row in rows:
        trader_dict = dict(row)
        trader_dict["trader_id"] = str(trader_dict["id"])
        trader_dict["user_id"] = str(trader_dict["id"])
        results.append(trader_dict)
    return results


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


# ============================================================================
# SINGLE-TIER SUBSCRIPTION (NEW)
# ============================================================================

from pydantic import BaseModel

@router.post("/subscribe/{trader_id}/paid")
async def subscribe_paid(
    trader_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Subscribe to a trader's single-tier paid subscription
    This initiates payment flow - actual subscription created after payment
    """
    user = get_current_user(request)
    user_id = user["id"]

    if user_id == trader_id:
        raise HTTPException(status_code=400, detail="Cannot subscribe to yourself")

    # Check if trader exists and get their subscription price
    trader = await db.fetchrow(
        """
        SELECT tp.subscription_price, up.username
        FROM trader_profiles tp
        JOIN user_profiles up ON tp.user_id = up.user_id
        WHERE tp.user_id = $1
        """,
        trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    if trader["subscription_price"] is None or float(trader["subscription_price"]) <= 0:
        raise HTTPException(status_code=400, detail="Trader has not set a subscription price")

    # Check if already subscribed
    existing = await db.fetchrow(
        """
        SELECT * FROM trader_subscriptions
        WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
        """,
        user_id,
        trader_id
    )

    if existing and existing.get("subscription_type") == "paid":
        raise HTTPException(status_code=400, detail="Already subscribed to this trader")

    return {
        "success": True,
        "trader_id": trader_id,
        "trader_username": trader["username"],
        "price": float(trader["subscription_price"]),
        "currency": "GBP",
        "message": "Use /api/billing/create-checkout-session with traderId to complete payment"
    }


@router.get("/{trader_id}/subscription-price")
async def get_subscription_price(
    trader_id: str,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get trader's subscription price
    """
    trader = await db.fetchrow(
        """
        SELECT subscription_price, username
        FROM trader_profiles tp
        JOIN user_profiles up ON tp.user_id = up.user_id
        WHERE tp.user_id = $1
        """,
        trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    return {
        "trader_id": trader_id,
        "username": trader["username"],
        "subscription_price": float(trader["subscription_price"]) if trader["subscription_price"] else 0,
        "currency": "GBP",
        "has_paid_subscription": trader["subscription_price"] and float(trader["subscription_price"]) > 0
    }


@router.get("/trader/{trader_id}/subscription-stats")
async def get_trader_sub_stats(
    trader_id: str,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get subscription statistics for a trader profile
    """
    if trader_id in {"undefined", "null", ""}:
        raise HTTPException(status_code=400, detail="Trader id required")
    try:
        # Total active subscribers
        total = await db.fetchval(
            """
            SELECT COUNT(*) FROM trader_subscriptions
            WHERE trader_id = $1 AND is_active = TRUE
            """,
            trader_id
        )

        # Founding subscribers
        founding = await db.fetchval(
            """
            SELECT COUNT(*) FROM trader_subscriptions
            WHERE trader_id = $1 AND is_founding_subscriber = TRUE AND is_active = TRUE
            """,
            trader_id
        )

        # Tier breakdown
        tier_breakdown = await db.fetch(
            """
            SELECT subscription_type, COUNT(*) as count
            FROM trader_subscriptions
            WHERE trader_id = $1 AND is_active = TRUE
            GROUP BY subscription_type
            """,
            trader_id
        )

        breakdown = {row["subscription_type"]: row["count"] for row in tier_breakdown}

        # Active percentage (engaged in last 7 days)
        active_count = await db.fetchval(
            """
            SELECT COUNT(DISTINCT s.subscriber_id) FROM trader_subscriptions s
            WHERE s.trader_id = $1 AND s.is_active = TRUE
            AND s.subscriber_id IN (
                SELECT user_id FROM post_reactions WHERE created_at > NOW() - INTERVAL '7 days'
                UNION
                SELECT user_id FROM post_comments WHERE created_at > NOW() - INTERVAL '7 days'
            )
            """,
            trader_id
        )
    except asyncpg_exceptions.UndefinedTableError:
        return {
            "total": 0,
            "active_percentage": 0,
            "founding_count": 0,
            "tier_breakdown": {},
        }

    active_pct = (active_count / total * 100) if total > 0 else 0

    return {
        "total": total or 0,
        "active_percentage": round(active_pct, 1),
        "founding_count": founding or 0,
        "tier_breakdown": breakdown
    }


# ============================================================================
# TIPS & BOOSTS
# ============================================================================

class TipRequest(BaseModel):
    post_id: int
    amount: float


class TipConfirmRequest(BaseModel):
    session_id: str


@router.post("/tip")
async def tip_post(
    tip: TipRequest,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Start a tip/boost payment for a trader's post.

    This used to write straight to post_tips with no charge ever taking
    place. It now mirrors the Stripe Connect checkout-session pattern used
    by the real paid-subscription/content-purchase flows: we create a
    Checkout Session for the tip amount and only record the tip (via
    /tip/confirm) once Stripe confirms the payment actually happened.
    """
    user = get_current_user(request)
    user_id = user["id"]

    if tip.amount <= 0 or tip.amount > 100:
        raise HTTPException(status_code=400, detail="Tip amount must be between 0 and 100")

    # Get post author and their payment account
    post = await db.fetchrow(
        """
        SELECT sp.user_id, tp.stripe_connect_account_id, tp.stripe_charges_enabled
        FROM social_posts sp
        LEFT JOIN trader_profiles tp ON sp.user_id = tp.user_id
        WHERE sp.id = $1
        """,
        tip.post_id
    )

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    post_author = post["user_id"]

    if str(post_author) == str(user_id):
        raise HTTPException(status_code=400, detail="Cannot tip your own post")

    if not post["stripe_connect_account_id"] or not post["stripe_charges_enabled"]:
        raise HTTPException(
            status_code=400,
            detail="This creator has not set up their payment account yet"
        )

    amount_cents = int(round(tip.amount * 100))
    # Matches the 'tip' row in platform_fees (5%)
    platform_fee_cents = int(round(amount_cents * 0.05))

    frontend_url = os.getenv("FRONTEND_URL", "http://localhost:3000")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=['card'],
            line_items=[{
                'price_data': {
                    'currency': 'gbp',
                    'product_data': {
                        'name': f"Tip for post #{tip.post_id}",
                    },
                    'unit_amount': amount_cents,
                },
                'quantity': 1,
            }],
            mode='payment',
            payment_intent_data={
                'application_fee_amount': platform_fee_cents,
                'transfer_data': {
                    'destination': post["stripe_connect_account_id"],
                },
            },
            success_url=f"{frontend_url}/trader/{post_author}?tipSession={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{frontend_url}/trader/{post_author}?tip=cancelled",
            metadata={
                'type': 'post_tip',
                'post_id': str(tip.post_id),
                'from_user_id': str(user_id),
                'to_user_id': str(post_author),
                'amount': str(tip.amount),
            }
        )
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Stripe error: {str(e)}")

    return {
        "success": True,
        "checkoutUrl": session.url,
        "sessionId": session.id,
        "message": "Redirect user to checkoutUrl to complete the tip payment"
    }


@router.post("/tip/confirm")
async def confirm_tip(
    payload: TipConfirmRequest,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Confirm a tip payment after the user returns from Stripe Checkout and
    record it in post_tips. Payment is verified directly against Stripe's
    API (never trusted from the client), and confirmation is idempotent so
    a repeated/duplicate call can't double-credit the tip.
    """
    user = get_current_user(request)
    user_id = user["id"]

    try:
        session = stripe.checkout.Session.retrieve(payload.session_id)
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=400, detail=f"Stripe error: {str(e)}")

    if session.payment_status != "paid":
        raise HTTPException(status_code=400, detail="Payment not completed")

    metadata = session.metadata or {}
    if metadata.get("type") != "post_tip":
        raise HTTPException(status_code=400, detail="Invalid checkout session")

    if metadata.get("from_user_id") != str(user_id):
        raise HTTPException(status_code=403, detail="This checkout session does not belong to you")

    post_id = int(metadata["post_id"])
    post_author = metadata["to_user_id"]
    amount = float(metadata["amount"])
    payment_intent_id = session.payment_intent

    # Idempotent insert - avoid double-crediting if confirm is called more than once
    existing = await db.fetchval(
        "SELECT id FROM post_tips WHERE stripe_payment_intent_id = $1",
        payment_intent_id
    )

    if not existing:
        await db.execute(
            """
            INSERT INTO post_tips (
                post_id, from_user_id, to_user_id, amount,
                processed, stripe_payment_intent_id
            )
            VALUES ($1, $2, $3, $4, TRUE, $5)
            """,
            post_id,
            user_id,
            post_author,
            amount,
            payment_intent_id
        )

        # Notification
        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message,
                related_user_id, related_post_id
            )
            VALUES ($1, 'post_tip', 'New Tip!', $2, $3, $4)
            """,
            post_author,
            f"Someone tipped you ${amount:.2f}!",
            user_id,
            post_id
        )

    # Get total tips for post
    total_tips = await db.fetchval(
        "SELECT COALESCE(SUM(amount), 0) FROM post_tips WHERE post_id = $1",
        post_id
    )

    return {
        "success": True,
        "post_id": post_id,
        "total_tips": float(total_tips),
        "message": "Tip sent!"
    }


# ============================================================================
# SAVED POSTS
# ============================================================================

@router.post("/save-post/{post_id}")
async def save_post(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Save a post to personal library
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if already saved
    existing = await db.fetchval(
        "SELECT id FROM saved_posts WHERE user_id = $1 AND post_id = $2",
        user_id,
        post_id
    )

    if existing:
        raise HTTPException(status_code=400, detail="Post already saved")

    # Save post
    await db.execute(
        "INSERT INTO saved_posts (user_id, post_id) VALUES ($1, $2)",
        user_id,
        post_id
    )

    return {"success": True, "message": "Post saved to library"}


@router.delete("/save-post/{post_id}")
async def unsave_post(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Remove post from saved library
    """
    user = get_current_user(request)
    user_id = user["id"]

    result = await db.execute(
        "DELETE FROM saved_posts WHERE user_id = $1 AND post_id = $2",
        user_id,
        post_id
    )

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Saved post not found")

    return {"success": True, "message": "Post removed from library"}


@router.get("/saved-posts")
async def get_saved_posts(
    request: Request,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0),
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get user's saved posts library
    """
    user = get_current_user(request)
    user_id = user["id"]

    posts = await db.fetch(
        """
        SELECT
            sp.saved_at,
            p.*,
            up.username as author_username,
            up.avatar_url as author_avatar
        FROM saved_posts sp
        JOIN social_posts p ON sp.post_id = p.id
        JOIN user_profiles up ON p.user_id = up.user_id
        WHERE sp.user_id = $1
        ORDER BY sp.saved_at DESC
        LIMIT $2 OFFSET $3
        """,
        user_id,
        limit,
        offset
    )

    return {
        "posts": [dict(row) for row in posts],
        "total": len(posts)
    }
