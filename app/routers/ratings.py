"""
Trader Ratings Router - Rate and review traders
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List
import asyncpg
from asyncpg import exceptions as asyncpg_exceptions

from app.models.social import (
    RatingCreate,
    RatingUpdate,
    Rating,
    RatingWithAuthor,
)
from app.db import get_pool

router = APIRouter(prefix="/api/ratings", tags=["Ratings"])


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


@router.post("/rate", response_model=Rating)
async def rate_trader(
    rating: RatingCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Rate a trader (1-5 stars) with optional review
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Can't rate yourself
    if user_id == rating.trader_id:
        raise HTTPException(status_code=400, detail="Cannot rate yourself")

    # Check if trader exists and is actually a trader
    trader = await db.fetchrow(
        "SELECT account_type FROM users WHERE id = $1",
        rating.trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    if trader["account_type"] != "trader":
        raise HTTPException(status_code=400, detail="User is not a trader")

    # Check if already rated
    existing = await db.fetchrow(
        """
        SELECT * FROM trader_ratings
        WHERE trader_id = $1 AND rater_id = $2
        """,
        rating.trader_id,
        user_id
    )

    if existing:
        # Update existing rating
        row = await db.fetchrow(
            """
            UPDATE trader_ratings
            SET rating = $1, review = $2, updated_at = NOW()
            WHERE id = $3
            RETURNING *
            """,
            rating.rating,
            rating.review,
            existing["id"]
        )
    else:
        # Create new rating
        row = await db.fetchrow(
            """
            INSERT INTO trader_ratings (trader_id, rater_id, rating, review)
            VALUES ($1, $2, $3, $4)
            RETURNING *
            """,
            rating.trader_id,
            user_id,
            rating.rating,
            rating.review
        )

        # Create notification for trader
        await db.execute(
            """
            INSERT INTO notifications (
                user_id, notification_type, title, message, related_user_id
            )
            VALUES ($1, 'new_rating', 'New Rating', $2, $3)
            """,
            rating.trader_id,
            f"You received a {rating.rating}-star rating!",
            user_id
        )

    return dict(row)


@router.get("/trader/{trader_id}", response_model=List[RatingWithAuthor])
async def get_trader_ratings(
    trader_id: int,
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get all ratings for a trader
    """
    # Check if trader exists
    trader = await db.fetchval(
        "SELECT id FROM users WHERE id = $1 AND account_type = 'trader'",
        trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    query = """
        SELECT
            tr.*,
            up.username as rater_username,
            up.avatar_url as rater_avatar
        FROM trader_ratings tr
        JOIN user_profiles up ON tr.rater_id = up.user_id
        WHERE tr.trader_id = $1
        ORDER BY tr.created_at DESC
        LIMIT $2 OFFSET $3
    """

    rows = await db.fetch(query, trader_id, limit, offset)
    return [dict(row) for row in rows]


@router.get("/trader/{trader_id}/summary")
async def get_trader_rating_summary(
    trader_id: int,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get rating summary for a trader (average, distribution, etc.)
    """
    # Check if trader exists
    trader = await db.fetchval(
        "SELECT id FROM users WHERE id = $1 AND account_type = 'trader'",
        trader_id
    )

    if not trader:
        raise HTTPException(status_code=404, detail="Trader not found")

    # Get rating statistics
    stats = await db.fetchrow(
        """
        SELECT
            COALESCE(ROUND(AVG(rating)::numeric, 2), 0) as avg_rating,
            COUNT(*) as total_ratings,
            COUNT(CASE WHEN rating = 5 THEN 1 END) as five_stars,
            COUNT(CASE WHEN rating = 4 THEN 1 END) as four_stars,
            COUNT(CASE WHEN rating = 3 THEN 1 END) as three_stars,
            COUNT(CASE WHEN rating = 2 THEN 1 END) as two_stars,
            COUNT(CASE WHEN rating = 1 THEN 1 END) as one_star
        FROM trader_ratings
        WHERE trader_id = $1
        """,
        trader_id
    )

    return dict(stats)


@router.get("/my-rating/{trader_id}")
async def get_my_rating(
    trader_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get current user's rating for a trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    rating = await db.fetchrow(
        """
        SELECT * FROM trader_ratings
        WHERE trader_id = $1 AND rater_id = $2
        """,
        trader_id,
        user_id
    )

    if not rating:
        return {"has_rated": False, "rating": None}

    return {
        "has_rated": True,
        "rating": dict(rating)
    }


@router.delete("/rating/{trader_id}")
async def delete_rating(
    trader_id: int,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Delete user's rating for a trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    result = await db.execute(
        """
        DELETE FROM trader_ratings
        WHERE trader_id = $1 AND rater_id = $2
        """,
        trader_id,
        user_id
    )

    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Rating not found")

    return {"message": "Rating deleted"}


@router.get("/leaderboard")
async def get_top_rated_traders(
    limit: int = Query(20, ge=1, le=100),
    min_ratings: int = Query(5, ge=1),
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get top-rated traders leaderboard
    Only includes traders with minimum number of ratings
    """
    query = """
        SELECT
            u.id,
            up.username,
            up.avatar_url,
            tp.bio,
            tp.verified,
            tp.total_followers,
            tp.total_posts,
            tp.avg_rating,
            tp.total_ratings
        FROM users u
        JOIN user_profiles up ON u.id = up.user_id
        JOIN trader_profiles tp ON u.id = tp.user_id
        WHERE u.account_type = 'trader'
            AND tp.total_ratings >= $1
        ORDER BY
            tp.avg_rating DESC,
            tp.total_ratings DESC,
            tp.total_followers DESC
        LIMIT $2
    """

    try:
        rows = await db.fetch(query, min_ratings, limit)
    except asyncpg_exceptions.UndefinedTableError:
        return []
    return [dict(row) for row in rows]
