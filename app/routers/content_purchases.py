"""
Content Purchases Router - Handle one-off content purchases
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from typing import List, Optional
import asyncpg
from decimal import Decimal

from app.models.social import (
    ContentPurchaseCreate,
    ContentPurchase,
)
from app.db import get_db

router = APIRouter(prefix="/api/content-purchases", tags=["Content Purchases"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


@router.post("/check-access/{post_id}")
async def check_content_access(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Check if user has access to content (purchased, subscribed, or free)
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        username = user.get("username")
        is_admin = username == "whatthefut#0"
    except HTTPException:
        user_id = None
        username = None
        is_admin = False

    # Get post details
    post = await db.fetchrow(
        """
        SELECT id, user_id, is_premium, requires_purchase, price
        FROM social_posts
        WHERE id = $1
        """,
        post_id
    )

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # If not logged in, can only access free content
    if not user_id:
        has_access = not post["is_premium"] and not post["requires_purchase"]
        return {
            "has_access": has_access,
            "is_author": False,
            "access_type": "free" if has_access else "restricted",
            "requires_subscription": post["is_premium"],
            "requires_purchase": post["requires_purchase"],
            "price": float(post["price"]) if post["price"] else None
        }

    # Check access using database function (skip for admin)
    if is_admin:
        has_access = True
    else:
        has_access = await db.fetchval(
            "SELECT user_can_access_content($1, $2)",
            user_id,
            post_id
        )

    # Check if user is author
    is_author = (str(post["user_id"]) == str(user_id))

    # Determine access type
    access_type = "free"
    if is_admin:
        access_type = "admin"
    elif is_author:
        access_type = "author"
    elif post["requires_purchase"]:
        # Check if purchased
        purchased = await db.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM content_purchases
                WHERE user_id = $1 AND post_id = $2 AND status = 'completed'
            )
            """,
            user_id,
            post_id
        )
        if purchased:
            access_type = "purchased"
        else:
            access_type = "requires_purchase"
    elif post["is_premium"]:
        # Check if subscribed
        subscribed = await db.fetchval(
            """
            SELECT EXISTS(
                SELECT 1 FROM trader_subscriptions
                WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
            )
            """,
            user_id,
            post["user_id"]
        )
        if subscribed:
            access_type = "subscribed"
        else:
            access_type = "requires_subscription"

    return {
        "has_access": has_access,
        "is_author": is_author,
        "access_type": access_type,
        "requires_subscription": post["is_premium"],
        "requires_purchase": post["requires_purchase"],
        "price": float(post["price"]) if post["price"] else None
    }


@router.get("/my-purchases")
async def get_my_purchases(
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get list of content purchased by current user
    """
    user = get_current_user(request)
    user_id = user["id"]

    purchases = await db.fetch(
        """
        SELECT
            cp.*,
            sp.title,
            sp.post_type,
            sp.content,
            up.username as author_username,
            up.avatar_url as author_avatar
        FROM content_purchases cp
        JOIN social_posts sp ON cp.post_id = sp.id
        JOIN user_profiles up ON sp.user_id = up.user_id
        WHERE cp.user_id = $1 AND cp.status = 'completed'
        ORDER BY cp.purchased_at DESC
        """,
        user_id
    )

    return {
        "purchases": [dict(row) for row in purchases],
        "total": len(purchases)
    }


@router.get("/post/{post_id}/purchases")
async def get_post_purchases(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Get purchase statistics for a post (author only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is author
    post_author = await db.fetchval(
        "SELECT user_id FROM social_posts WHERE id = $1",
        post_id
    )

    if not post_author or str(post_author) != str(user_id):
        raise HTTPException(status_code=403, detail="Only post author can view purchase statistics")

    stats = await db.fetchrow(
        """
        SELECT
            COUNT(*) as total_purchases,
            COALESCE(SUM(amount), 0) as total_revenue,
            COUNT(DISTINCT user_id) as unique_buyers
        FROM content_purchases
        WHERE post_id = $1 AND status = 'completed'
        """,
        post_id
    )

    return {
        "post_id": post_id,
        "total_purchases": stats["total_purchases"],
        "total_revenue": float(stats["total_revenue"]),
        "unique_buyers": stats["unique_buyers"]
    }


@router.post("/refund/{purchase_id}")
async def refund_purchase(
    purchase_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    """
    Refund a content purchase (content author only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Get purchase and check authorization
    purchase = await db.fetchrow(
        """
        SELECT cp.*, sp.user_id as author_id
        FROM content_purchases cp
        JOIN social_posts sp ON cp.post_id = sp.id
        WHERE cp.id = $1
        """,
        purchase_id
    )

    if not purchase:
        raise HTTPException(status_code=404, detail="Purchase not found")

    if str(purchase["author_id"]) != str(user_id):
        raise HTTPException(status_code=403, detail="Only content author can issue refunds")

    if purchase["status"] != "completed":
        raise HTTPException(status_code=400, detail="Can only refund completed purchases")

    # Mark as refunded (actual refund processing should happen via payment provider)
    await db.execute(
        """
        UPDATE content_purchases
        SET status = 'refunded', refunded_at = NOW()
        WHERE id = $1
        """,
        purchase_id
    )

    # Create notification for buyer
    await db.execute(
        """
        INSERT INTO notifications (
            user_id, notification_type, title, message,
            related_post_id
        )
        VALUES ($1, 'content_purchase', 'Purchase Refunded', $2, $3)
        """,
        purchase["user_id"],
        "Your content purchase has been refunded",
        purchase["post_id"]
    )

    return {
        "success": True,
        "message": "Purchase refunded successfully",
        "note": "Payment provider refund must be processed separately"
    }
