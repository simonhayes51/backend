"""
Social Feed Router - Trading tips, predictions, and social posts
"""
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from typing import Optional, List
from datetime import datetime, timedelta
from decimal import Decimal
import asyncpg
from asyncpg import exceptions as asyncpg_exceptions
from pydantic import BaseModel, Field

from app.models.social import (
    SocialPostCreate,
    SocialPostUpdate,
    SocialPost,
    SocialPostWithAuthor,
    FeedResponse,
)
from app.db import get_db

router = APIRouter(prefix="/api/feed", tags=["Social Feed"])
social_router = APIRouter(prefix="/api/social", tags=["Social Feed"])


def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


async def table_exists(db: asyncpg.Connection, table_name: str) -> bool:
    return await db.fetchval("SELECT to_regclass($1)", table_name) is not None


async def column_exists(db: asyncpg.Connection, table_name: str, column_name: str) -> bool:
    return await db.fetchval(
        """
        SELECT EXISTS(
            SELECT 1
            FROM information_schema.columns
            WHERE table_name = $1 AND column_name = $2
        )
        """,
        table_name,
        column_name,
    )


async def ensure_tables_exist(db: asyncpg.Connection, table_names: List[str]) -> bool:
    for table_name in table_names:
        if not await table_exists(db, table_name):
            return False
    return True


class FeedPostCreatePayload(BaseModel):
    title: Optional[str] = None
    content: str = Field(..., min_length=1, max_length=5000)
    post_type: str
    player_name: Optional[str] = None
    player_card_id: Optional[str] = None
    buy_range_min: Optional[Decimal] = None
    buy_range_max: Optional[Decimal] = None
    sell_target: Optional[Decimal] = None
    sell_at: Optional[datetime] = None
    confidence_level: Optional[int] = Field(None, ge=1, le=100)
    premium: bool = False  # Subscriber-only content
    requires_purchase: bool = False  # One-off purchase content
    price: Optional[Decimal] = Field(None, ge=0)  # Price for one-off purchase
    expires_in_hours: Optional[int] = None
    image_url: Optional[str] = None  # Add image_url field
    tags: Optional[List[str]] = None  # Add tags field


class FeedPostUpdatePayload(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = Field(None, min_length=1, max_length=5000)
    post_type: Optional[str] = None
    player_name: Optional[str] = None
    player_card_id: Optional[str] = None
    buy_price: Optional[Decimal] = None
    buy_range_min: Optional[Decimal] = None
    buy_range_max: Optional[Decimal] = None
    sell_target: Optional[Decimal] = None
    sell_at: Optional[datetime] = None
    confidence_level: Optional[int] = Field(None, ge=1, le=100)
    premium: Optional[bool] = None
    requires_purchase: Optional[bool] = None
    price: Optional[Decimal] = Field(None, ge=0)
    expires_in_hours: Optional[int] = None
    image_url: Optional[str] = None
    tags: Optional[List[str]] = None


def _expires_at_from_hours(expires_in_hours: Optional[int]) -> Optional[datetime]:
    if expires_in_hours is None:
        return None
    try:
        hours = int(expires_in_hours)
    except (TypeError, ValueError):
        return None
    if hours <= 0:
        return None
    return datetime.utcnow() + timedelta(hours=hours)


def _format_post(row: dict) -> dict:
    post = dict(row)
    author_snapshot = {
        "id": post.get("user_id"),
        "trader_id": str(post.get("user_id")) if post.get("user_id") is not None else None,
        "username": post.get("username"),
        "avatar_url": post.get("avatar_url"),
        "is_verified": post.get("verified"),
        "bio": post.get("trader_bio"),
        "specialties": post.get("trader_specialties"),
        "subscription_price": post.get("trader_subscription_price"),
    }
    post["title"] = post.get("title")
    post["author"] = author_snapshot
    post["sell_signal"] = bool(post.get("sell_target") or post.get("sell_at"))
    post["stats"] = {
        "likes": post.get("likes_count"),
        "dislikes": post.get("dislikes_count"),
        "comments": post.get("comments_count"),
        "views": post.get("views_count"),
        "shares": post.get("shares_count"),
    }
    return post


async def _attach_author(db: asyncpg.Connection, post: dict) -> dict:
    try:
        author = await db.fetchrow(
            """
            SELECT
                up.username,
                up.avatar_url,
                COALESCE(tp.verified, FALSE) as verified,
                tp.bio as trader_bio,
                tp.specialties as trader_specialties,
                tp.subscription_price as trader_subscription_price
            FROM users u
            LEFT JOIN user_profiles up ON u.id::text = up.user_id::text
            LEFT JOIN trader_profiles tp ON u.id::text = tp.user_id::text
            WHERE u.id::text = $1::text
            """,
            post.get("user_id"),
        )
    except asyncpg.UndefinedTableError:
        # Tables might not exist yet
        return post

    if author:
        post["username"] = author["username"]
        post["avatar_url"] = author["avatar_url"]
        post["verified"] = author["verified"]
        post["trader_bio"] = author["trader_bio"]
        post["trader_specialties"] = author["trader_specialties"]
        post["trader_subscription_price"] = author["trader_subscription_price"]
    return post


@router.post("/posts", response_model=SocialPostWithAuthor)
async def create_post(
    post: SocialPostCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Create a new social post (traders only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Ensure user has username populated (fallback to discord username or id)
    try:
        username = await db.fetchval("SELECT username FROM user_profiles WHERE user_id = $1", user_id)
    except asyncpg.UndefinedTableError:
        username = None

    if not username:
         # Fallback to user ID
        username = f"User_{str(user_id)[:8]}"

    # Check if user is a trader
    try:
        account_type = await db.fetchval(
            "SELECT account_type FROM users WHERE id = $1",
            user_id
        )
    except asyncpg.UndefinedColumnError:
        account_type = None

    is_trader = account_type == "trader"
    if not is_trader:
        is_trader = await db.fetchval(
            "SELECT EXISTS(SELECT 1 FROM trader_profiles WHERE user_id = $1)",
            user_id
        )

    if not is_trader:
        raise HTTPException(
            status_code=403,
            detail="Only traders can create posts. Upgrade to a trader account."
        )

    has_title = await column_exists(db, "social_posts", "title")
    has_sell_at = await column_exists(db, "social_posts", "sell_at")
    has_image_url = await column_exists(db, "social_posts", "image_url")
    has_requires_purchase = await column_exists(db, "social_posts", "requires_purchase")
    has_price = await column_exists(db, "social_posts", "price")

    columns = [
        "user_id",
        "post_type",
        "content",
        "player_name",
        "player_card_id",
        "buy_range_min",
        "buy_range_max",
        "sell_target",
        "confidence_level",
        "tags",
        "is_premium",
        "expires_at",
    ]
    values = [
        user_id,
        post.post_type,
        post.content,
        post.player_name,
        post.player_card_id,
        post.buy_range_min,
        post.buy_range_max,
        post.sell_target,
        post.confidence_level,
        post.tags or [],
        post.is_premium,
        post.expires_at,
    ]

    if has_title:
        columns.append("title")
        values.append(post.title)

    if has_sell_at:
        columns.append("sell_at")
        values.append(post.sell_at)

    if has_image_url:
        columns.append("image_url")
        values.append(post.image_url)

    if has_requires_purchase:
        columns.append("requires_purchase")
        values.append(post.requires_purchase)

    if has_price:
        columns.append("price")
        values.append(post.price)

    placeholders = ", ".join(f"${idx}" for idx in range(1, len(values) + 1))
    query = f"""
        INSERT INTO social_posts (
            {', '.join(columns)}
        )
        VALUES ({placeholders})
        RETURNING *
    """

    row = await db.fetchrow(query, *values)

    post_dict = dict(row)
    post_dict = await _attach_author(db, post_dict)
    return _format_post(post_dict)


@router.post("", response_model=SocialPostWithAuthor)
async def create_post_root(
    payload: FeedPostCreatePayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    social_post = SocialPostCreate(
        post_type=payload.post_type,
        title=payload.title,
        content=payload.content,
        player_name=payload.player_name,
        player_card_id=payload.player_card_id,
        buy_range_min=payload.buy_range_min,
        buy_range_max=payload.buy_range_max,
        sell_target=payload.sell_target,
        sell_at=payload.sell_at,
        confidence_level=payload.confidence_level,
        is_premium=payload.premium,
        expires_at=_expires_at_from_hours(payload.expires_in_hours),
        image_url=payload.image_url,  # Pass image_url
        tags=payload.tags or [],  # Pass tags
    )
    return await create_post(post=social_post, request=request, db=db)


@router.get("", response_model=FeedResponse)
async def get_feed_root(
    request: Request,
    feed_type: str = Query("all", description="all, trades, predictions"),
    trader_id: Optional[str] = Query(None, description="Filter by specific trader"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get social feed posts (root alias)
    """
    return await get_feed(
        request=request,
        feed_type=feed_type,
        trader_id=trader_id,
        offset=offset,
        limit=limit,
        db=db,
    )


@router.get("/posts", response_model=FeedResponse)
async def get_feed(
    request: Request,
    feed_type: str = Query("all", description="all, trades, predictions"),
    trader_id: Optional[str] = Query(None, description="Filter by specific trader"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get social feed posts
    - Returns posts from subscribed traders if authenticated
    - Returns all public posts if not authenticated or feed_type=all
    - Can filter by post type (trades/predictions)
    - Can filter by specific trader
    """
    try:
        user = get_current_user(request)
        user_id = user["id"]
        is_authenticated = True
    except HTTPException:
        user_id = None
        is_authenticated = False

    try:
        # Build the query based on filters
        conditions = []
        params = []
        param_idx = 1

        # Filter by trader if specified
        if trader_id:
            conditions.append(f"sp.user_id = ${param_idx}")
            params.append(trader_id)
            param_idx += 1

        # Filter by post type
        if feed_type == "trades":
            conditions.append(f"sp.post_type = ${param_idx}")
            params.append("quick_flip")
            param_idx += 1
        elif feed_type == "predictions":
            conditions.append(f"sp.post_type = ${param_idx}")
            params.append("prediction")
            param_idx += 1

        # Filter by subscriptions if authenticated and not viewing a specific trader
        if is_authenticated and not trader_id and feed_type != "all":
            conditions.append(f"""
                sp.user_id IN (
                    SELECT trader_id FROM trader_subscriptions
                    WHERE subscriber_id = ${param_idx} AND is_active = TRUE
                )
            """)
            params.append(user_id)
            param_idx += 1

        # Only show non-premium posts or posts from traders user is subscribed to
        if is_authenticated:
            conditions.append(f"""
                (sp.is_premium = FALSE OR sp.user_id IN (
                    SELECT trader_id FROM trader_subscriptions
                    WHERE subscriber_id = ${param_idx} AND is_active = TRUE
                ))
            """)
            params.append(user_id)
            param_idx += 1
        else:
            conditions.append("sp.is_premium = FALSE")

        # Don't show expired posts
        conditions.append("(sp.expires_at IS NULL OR sp.expires_at > NOW())")

        where_clause = " AND ".join(conditions) if conditions else "TRUE"

        # Get total count
        count_query = f"""
            SELECT COUNT(*)
            FROM social_posts sp
            WHERE {where_clause}
        """
        total = await db.fetchval(count_query, *params)

        # Get posts with author info
        params.extend([limit, offset])
        
        # Try to get posts with full author info, fallback to basic if tables don't exist
        try:
            query = f"""
                SELECT
                    sp.*,
                    COALESCE(up.username, 'Anonymous') as username,
                    up.avatar_url,
                    COALESCE(tp.verified, FALSE) as verified,
                    COALESCE(tp.avg_rating, 0) as avg_rating,
                    COALESCE(tp.total_followers, 0) as total_followers,
                    tp.bio as trader_bio,
                    COALESCE(tp.specialties, ARRAY[]::text[]) as trader_specialties,
                    COALESCE(tp.subscription_price, 0) as trader_subscription_price,
                    CASE WHEN sp.user_id::text = ${param_idx + 2}::text THEN TRUE ELSE FALSE END as is_author
                FROM social_posts sp
                LEFT JOIN users u ON sp.user_id::text = u.id::text
                LEFT JOIN user_profiles up ON sp.user_id::text = up.user_id::text
                LEFT JOIN trader_profiles tp ON u.id::text = tp.user_id::text
                WHERE {where_clause}
                ORDER BY COALESCE(sp.updated_at, sp.created_at) DESC
                LIMIT ${param_idx} OFFSET ${param_idx + 1}
            """
            
            # Add user_id for is_author check (or NULL if not authenticated)
            query_params = params + [user_id if is_authenticated else None]
            rows = await db.fetch(query, *query_params)
        except Exception as e:
            # Log the error and return empty feed
            print(f"Error fetching posts: {e}")
            return {
                "posts": [],
                "total": 0,
                "has_more": False,
                "offset": offset,
                "limit": limit
            }
    except asyncpg_exceptions.UndefinedTableError as e:
        print(f"Table not found: {e}")
        return {
            "posts": [],
            "total": 0,
            "has_more": False,
            "offset": offset,
            "limit": limit
        }
    except Exception as e:
        print(f"Unexpected error in get_feed: {e}")
        import traceback
        traceback.print_exc()
        return {
            "posts": [],
            "total": 0,
            "has_more": False,
            "offset": offset,
            "limit": limit
        }

    can_read_reactions = is_authenticated and await table_exists(
        db,
        "public.post_reactions"
    )
    posts = []
    for row in rows:
        post_dict = dict(row)

        # Get user's reaction to this post if authenticated
        if can_read_reactions:
            reaction = await db.fetchval(
                "SELECT reaction_type FROM post_reactions WHERE user_id = $1 AND post_id = $2",
                user_id,
                post_dict["id"]
            )
            post_dict["user_reaction"] = reaction
        else:
            post_dict["user_reaction"] = None

        posts.append(_format_post(post_dict))

    return {
        "posts": posts,
        "total": total,
        "has_more": offset + limit < total,
        "offset": offset,
        "limit": limit
    }


@social_router.get("/feed", response_model=FeedResponse)
async def get_social_feed(
    request: Request,
    feed_type: str = Query("all", description="all, trades, predictions"),
    trader_id: Optional[str] = Query(None, description="Filter by specific trader"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get social feed posts (social alias)
    """
    return await get_feed(
        request=request,
        feed_type=feed_type,
        trader_id=trader_id,
        offset=offset,
        limit=limit,
        db=db,
    )


@social_router.get("/posts", response_model=FeedResponse)
async def get_social_posts(
    request: Request,
    feed_type: str = Query("all", description="all, trades, predictions"),
    trader_id: Optional[str] = Query(None, description="Filter by specific trader"),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get social feed posts (posts alias)
    """
    return await get_feed(
        request=request,
        feed_type=feed_type,
        trader_id=trader_id,
        offset=offset,
        limit=limit,
        db=db,
    )


@social_router.post("/posts", response_model=SocialPostWithAuthor)
async def create_social_post(
    post: SocialPostCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Create a new social post (social alias)
    """
    return await create_post(post=post, request=request, db=db)


@social_router.post("/feed", response_model=SocialPostWithAuthor)
async def create_social_post_feed(
    payload: FeedPostCreatePayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await create_post_root(payload=payload, request=request, db=db)


@router.get("/posts/{post_id}", response_model=SocialPostWithAuthor)
async def get_post(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get a specific post by ID
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
            sp.*,
            up.username,
            up.avatar_url,
            COALESCE(tp.verified, FALSE) as verified,
            tp.avg_rating,
            tp.total_followers
        FROM social_posts sp
        LEFT JOIN users u ON sp.user_id::text = u.id::text
        LEFT JOIN user_profiles up ON sp.user_id::text = up.user_id::text
        LEFT JOIN trader_profiles tp ON u.id::text = tp.user_id::text
        WHERE sp.id = $1
    """

    row = await db.fetchrow(query, post_id)

    if not row:
        raise HTTPException(status_code=404, detail="Post not found")

    post_dict = dict(row)

    # Check content access (premium subscriber-only OR requires purchase)
    is_author = is_authenticated and user_id == post_dict["user_id"]

    # Check if content is restricted
    if (post_dict.get("is_premium") or post_dict.get("requires_purchase")) and not is_author:
        if not is_authenticated:
            # Not logged in
            if post_dict.get("requires_purchase"):
                raise HTTPException(
                    status_code=401,
                    detail="Login to purchase this content"
                )
            else:
                raise HTTPException(
                    status_code=401,
                    detail="Login and subscribe to view premium content"
                )

        # Check access using database function
        has_access = await db.fetchval(
            "SELECT user_can_access_content($1, $2)",
            user_id,
            post_id
        )

        if not has_access:
            # Determine specific error message
            if post_dict.get("requires_purchase"):
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "purchase_required",
                        "message": "Purchase required to view this content",
                        "price": float(post_dict.get("price", 0)),
                        "currency": "GBP"
                    }
                )
            else:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "subscription_required",
                        "message": "Subscribe to this trader to view premium content",
                        "subscription_price": float(post_dict.get("subscription_price", 0)) if post_dict.get("subscription_price") else None
                    }
                )

    # Get user's reaction if authenticated
    if is_authenticated:
        reaction = await db.fetchval(
            "SELECT reaction_type FROM post_reactions WHERE user_id = $1 AND post_id = $2",
            user_id,
            post_id
        )
        post_dict["user_reaction"] = reaction
        post_dict["is_author"] = user_id == post_dict["user_id"]
    else:
        post_dict["user_reaction"] = None
        post_dict["is_author"] = False

    return _format_post(post_dict)


@router.patch("/posts/{post_id}", response_model=SocialPostWithAuthor)
async def update_post(
    post_id: int,
    post_update: SocialPostUpdate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Update a post (author only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check ownership
    post_owner = await db.fetchval(
        "SELECT user_id FROM social_posts WHERE id = $1",
        post_id
    )

    if not post_owner:
        raise HTTPException(status_code=404, detail="Post not found")

    if post_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this post")

    # Build update query
    updates = []
    params = []
    param_idx = 1
    allowed_post_types = ["quick_flip", "prediction", "tip", "analysis"]
    has_title = await column_exists(db, "social_posts", "title")
    has_sell_at = await column_exists(db, "social_posts", "sell_at")
    has_image_url = await column_exists(db, "social_posts", "image_url")

    if post_update.title is not None and has_title:
        updates.append(f"title = ${param_idx}")
        params.append(post_update.title)
        param_idx += 1

    if post_update.content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(post_update.content)
        param_idx += 1

    post_type_set = False
    if post_update.post_type is not None:
        if post_update.post_type not in allowed_post_types:
            raise HTTPException(status_code=400, detail="Invalid post type")
        updates.append(f"post_type = ${param_idx}")
        params.append(post_update.post_type)
        param_idx += 1
        post_type_set = True

    if post_update.player_name is not None:
        updates.append(f"player_name = ${param_idx}")
        params.append(post_update.player_name)
        param_idx += 1

    if post_update.player_card_id is not None:
        updates.append(f"player_card_id = ${param_idx}")
        params.append(post_update.player_card_id)
        param_idx += 1

    if post_update.buy_price is not None:
        updates.append(f"buy_range_min = ${param_idx}")
        params.append(post_update.buy_price)
        param_idx += 1
        updates.append(f"buy_range_max = ${param_idx}")
        params.append(post_update.buy_price)
        param_idx += 1

    if post_update.buy_range_min is not None:
        updates.append(f"buy_range_min = ${param_idx}")
        params.append(post_update.buy_range_min)
        param_idx += 1

    if post_update.buy_range_max is not None:
        updates.append(f"buy_range_max = ${param_idx}")
        params.append(post_update.buy_range_max)
        param_idx += 1

    if (post_update.sell_target is not None or post_update.sell_at is not None) and not post_type_set:
        updates.append(f"post_type = ${param_idx}")
        params.append("quick_flip")
        param_idx += 1

    if post_update.sell_target is not None:
        updates.append(f"sell_target = ${param_idx}")
        params.append(post_update.sell_target)
        param_idx += 1

    if post_update.sell_at is not None and has_sell_at:
        updates.append(f"sell_at = ${param_idx}")
        params.append(post_update.sell_at)
        param_idx += 1

    if post_update.confidence_level is not None:
        updates.append(f"confidence_level = ${param_idx}")
        params.append(post_update.confidence_level)
        param_idx += 1

    if post_update.tags is not None:
        updates.append(f"tags = ${param_idx}")
        params.append(post_update.tags)
        param_idx += 1

    if post_update.image_url is not None and has_image_url:
        updates.append(f"image_url = ${param_idx}")
        params.append(post_update.image_url)
        param_idx += 1

    if post_update.is_premium is not None:
        updates.append(f"is_premium = ${param_idx}")
        params.append(post_update.is_premium)
        param_idx += 1

    if post_update.expires_at is not None:
        updates.append(f"expires_at = ${param_idx}")
        params.append(post_update.expires_at)
        param_idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    updates.append(f"updated_at = NOW()")
    params.append(post_id)

    query = f"""
        UPDATE social_posts
        SET {', '.join(updates)}
        WHERE id = ${param_idx}
        RETURNING *
    """

    row = await db.fetchrow(query, *params)
    post_dict = dict(row)
    post_dict = await _attach_author(db, post_dict)
    return _format_post(post_dict)


@router.post("/{post_id}")
async def update_post_root(
    post_id: int,
    payload: FeedPostUpdatePayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    user = get_current_user(request)
    user_id = user["id"]

    post_owner = await db.fetchval(
        "SELECT user_id FROM social_posts WHERE id = $1",
        post_id
    )

    if not post_owner:
        raise HTTPException(status_code=404, detail="Post not found")

    if post_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to edit this post")

    updates = []
    params = []
    param_idx = 1
    allowed_post_types = ["quick_flip", "prediction", "tip", "analysis"]
    has_title = await column_exists(db, "social_posts", "title")
    has_sell_at = await column_exists(db, "social_posts", "sell_at")
    has_image_url = await column_exists(db, "social_posts", "image_url")

    if payload.title is not None and has_title:
        updates.append(f"title = ${param_idx}")
        params.append(payload.title)
        param_idx += 1

    if payload.content is not None:
        updates.append(f"content = ${param_idx}")
        params.append(payload.content)
        param_idx += 1

    post_type_set = False
    if payload.post_type is not None:
        if payload.post_type not in allowed_post_types:
            raise HTTPException(status_code=400, detail="Invalid post type")
        updates.append(f"post_type = ${param_idx}")
        params.append(payload.post_type)
        param_idx += 1
        post_type_set = True

    if payload.player_name is not None:
        updates.append(f"player_name = ${param_idx}")
        params.append(payload.player_name)
        param_idx += 1

    if payload.player_card_id is not None:
        updates.append(f"player_card_id = ${param_idx}")
        params.append(payload.player_card_id)
        param_idx += 1

    if payload.buy_price is not None:
        updates.append(f"buy_range_min = ${param_idx}")
        params.append(payload.buy_price)
        param_idx += 1
        updates.append(f"buy_range_max = ${param_idx}")
        params.append(payload.buy_price)
        param_idx += 1

    if payload.buy_range_min is not None:
        updates.append(f"buy_range_min = ${param_idx}")
        params.append(payload.buy_range_min)
        param_idx += 1

    if payload.buy_range_max is not None:
        updates.append(f"buy_range_max = ${param_idx}")
        params.append(payload.buy_range_max)
        param_idx += 1

    if (payload.sell_target is not None or payload.sell_at is not None) and not post_type_set:
        updates.append(f"post_type = ${param_idx}")
        params.append("quick_flip")
        param_idx += 1

    if payload.sell_target is not None:
        updates.append(f"sell_target = ${param_idx}")
        params.append(payload.sell_target)
        param_idx += 1

    if payload.sell_at is not None and has_sell_at:
        updates.append(f"sell_at = ${param_idx}")
        params.append(payload.sell_at)
        param_idx += 1

    if payload.confidence_level is not None:
        updates.append(f"confidence_level = ${param_idx}")
        params.append(payload.confidence_level)
        param_idx += 1

    if payload.premium is not None:
        updates.append(f"is_premium = ${param_idx}")
        params.append(payload.premium)
        param_idx += 1

    if payload.expires_in_hours is not None:
        updates.append(f"expires_at = ${param_idx}")
        params.append(_expires_at_from_hours(payload.expires_in_hours))
        param_idx += 1

    if payload.image_url is not None and has_image_url:
        updates.append(f"image_url = ${param_idx}")
        params.append(payload.image_url)
        param_idx += 1

    if payload.tags is not None:
        updates.append(f"tags = ${param_idx}")
        params.append(payload.tags)
        param_idx += 1

    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")

    updates.append("updated_at = NOW()")
    params.append(post_id)

    query = f"""
        UPDATE social_posts
        SET {', '.join(updates)}
        WHERE id = ${param_idx}
        RETURNING *
    """

    row = await db.fetchrow(query, *params)
    post_dict = dict(row)
    post_dict = await _attach_author(db, post_dict)
    return _format_post(post_dict)


@social_router.post("/posts/{post_id}")
async def update_post_social_alias(
    post_id: int,
    payload: FeedPostUpdatePayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await update_post_root(
        post_id=post_id,
        payload=payload,
        request=request,
        db=db,
    )


@social_router.patch("/posts/{post_id}", response_model=SocialPostWithAuthor)
async def update_post_social_patch_alias(
    post_id: int,
    post_update: SocialPostUpdate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await update_post(
        post_id=post_id,
        post_update=post_update,
        request=request,
        db=db,
    )


@social_router.post("/posts/{post_id}/edit")
async def update_post_social_edit_alias(
    post_id: int,
    payload: FeedPostUpdatePayload,
    request: Request,
    db: asyncpg.Connection = Depends(get_db),
):
    return await update_post_root(
        post_id=post_id,
        payload=payload,
        request=request,
        db=db,
    )


@router.delete("/posts/{post_id}")
async def delete_post(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Delete a post (author only)
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check ownership
    post_owner = await db.fetchval(
        "SELECT user_id FROM social_posts WHERE id = $1",
        post_id
    )

    if not post_owner:
        raise HTTPException(status_code=404, detail="Post not found")

    if post_owner != user_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete this post")

    # Delete the post (cascade will handle reactions and comments)
    await db.execute("DELETE FROM social_posts WHERE id = $1", post_id)

    return {"message": "Post deleted successfully"}


@router.delete("/{post_id}")
async def delete_post_root(
    post_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    return await delete_post(post_id=post_id, request=request, db=db)


@router.get("/posts/{post_id}/stats")
async def get_post_stats(
    post_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get detailed engagement statistics for a post
    """
    post = await db.fetchrow(
        "SELECT * FROM social_posts WHERE id = $1",
        post_id
    )

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Get top reactors
    top_likers = await db.fetch(
        """
        SELECT u.username, u.avatar_url
        FROM post_reactions pr
        LEFT JOIN users u ON pr.user_id::text = u.id::text
        WHERE pr.post_id = $1 AND pr.reaction_type = 'like'
        ORDER BY pr.created_at DESC
        LIMIT 10
        """,
        post_id
    )

    return {
        "post_id": post_id,
        "likes_count": post["likes_count"],
        "dislikes_count": post["dislikes_count"],
        "comments_count": post["comments_count"],
        "views_count": post["views_count"],
        "shares_count": post["shares_count"],
        "top_likers": [dict(row) for row in top_likers],
        "created_at": post["created_at"],
        "engagement_rate": (
            (post["likes_count"] + post["comments_count"] + post["shares_count"]) /
            max(post["likes_count"] + post["dislikes_count"] + post["comments_count"] + post["shares_count"], 1)
        )
    }


@router.get("/debug/posts")
async def debug_posts(db: asyncpg.Connection = Depends(get_db)):
    """
    Debug endpoint to check social_posts table contents
    """
    rows = await db.fetch("""
        SELECT 
            id, 
            user_id, 
            post_type, 
            LEFT(content, 50) as content_preview,
            CASE 
                WHEN image_url IS NULL THEN 'NULL'
                WHEN image_url = '' THEN 'EMPTY'
                WHEN LENGTH(image_url) > 100 THEN 'HAS_IMAGE (' || LENGTH(image_url) || ' chars)'
                ELSE image_url
            END as image_status,
            created_at
        FROM social_posts
        ORDER BY created_at DESC
        LIMIT 10
    """)
    
    return {
        "total_posts": len(rows),
        "posts": [dict(row) for row in rows]
    }
