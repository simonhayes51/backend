"""
Trader Profile Management Router
"""
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from typing import List, Optional
from decimal import Decimal
import asyncpg

from app.models.social import (
    TraderProfile,
    TraderProfileCreate,
    TraderProfileUpdate,
    TraderPublicProfile,
    TraderAnalytics
)
from app.db import get_db

router = APIRouter(prefix="/api/traders", tags=["Traders"])

def get_current_user(request: Request):
    """Extract user from session"""
    if "user" not in request.session:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return request.session["user"]


@router.post("/upgrade", response_model=TraderProfile)
async def upgrade_to_trader(
    profile: TraderProfileCreate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Upgrade current user to trader account
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if already a trader
    current_type = await db.fetchval("SELECT account_type FROM users WHERE id = $1", user_id)
    if current_type == "trader":
        # Check if profile exists
        existing = await db.fetchrow("SELECT * FROM trader_profiles WHERE user_id = $1", user_id)
        if existing:
            raise HTTPException(status_code=400, detail="Already a trader")

    # Update user account type
    await db.execute("UPDATE users SET account_type = 'trader' WHERE id = $1", user_id)

    # Create trader profile
    query = """
        INSERT INTO trader_profiles (
            user_id, bio, specialties, verified, 
            subscription_price,
            tier_basic_price, tier_premium_price, tier_elite_price,
            tier_basic_cap, tier_premium_cap, tier_elite_cap,
            total_followers, total_posts, avg_rating, total_ratings
        )
        VALUES ($1, $2, $3, FALSE, $4, $5, $6, $7, $8, $9, $10, 0, 0, 0, 0)
        RETURNING *
    """
    
    row = await db.fetchrow(
        query, 
        user_id, 
        profile.bio, 
        profile.specialties, 
        profile.subscription_price,
        profile.tier_basic_price,
        profile.tier_premium_price,
        profile.tier_elite_price,
        profile.tier_basic_cap,
        profile.tier_premium_cap,
        profile.tier_elite_cap
    )

    return dict(row)


@router.get("/me", response_model=TraderProfile)
async def get_current_trader_profile(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get current user's trader profile
    """
    user = get_current_user(request)
    user_id = user["id"]

    row = await db.fetchrow("SELECT * FROM trader_profiles WHERE user_id = $1", user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Trader profile not found")
    
    return dict(row)


@router.put("/me", response_model=TraderProfile)
async def update_trader_profile(
    profile: TraderProfileUpdate,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Update current user's trader profile
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check existence
    existing = await db.fetchrow("SELECT * FROM trader_profiles WHERE user_id = $1", user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Trader profile not found")

    # Build dynamic update query
    fields = []
    values = []
    idx = 1

    if profile.bio is not None:
        fields.append(f"bio = ${idx}")
        values.append(profile.bio)
        idx += 1
    
    if profile.specialties is not None:
        fields.append(f"specialties = ${idx}")
        values.append(profile.specialties)
        idx += 1
        
    if profile.subscription_price is not None:
        fields.append(f"subscription_price = ${idx}")
        values.append(profile.subscription_price)
        idx += 1

    if profile.tier_basic_price is not None:
        fields.append(f"tier_basic_price = ${idx}")
        values.append(profile.tier_basic_price)
        idx += 1

    if profile.tier_premium_price is not None:
        fields.append(f"tier_premium_price = ${idx}")
        values.append(profile.tier_premium_price)
        idx += 1

    if profile.tier_elite_price is not None:
        fields.append(f"tier_elite_price = ${idx}")
        values.append(profile.tier_elite_price)
        idx += 1

    if profile.tier_basic_cap is not None:
        fields.append(f"tier_basic_cap = ${idx}")
        values.append(profile.tier_basic_cap)
        idx += 1

    if profile.tier_premium_cap is not None:
        fields.append(f"tier_premium_cap = ${idx}")
        values.append(profile.tier_premium_cap)
        idx += 1

    if profile.tier_elite_cap is not None:
        fields.append(f"tier_elite_cap = ${idx}")
        values.append(profile.tier_elite_cap)
        idx += 1

    if not fields:
        return dict(existing)

    values.append(user_id)
    query = f"""
        UPDATE trader_profiles
        SET {', '.join(fields)}, updated_at = NOW()
        WHERE user_id = ${idx}
        RETURNING *
    """
    
    row = await db.fetchrow(query, *values)
    return dict(row)


@router.get("/specialties", response_model=List[str])
async def get_specialties(db: asyncpg.Connection = Depends(get_db)):
    """
    Get list of all unique specialties
    """
    rows = await db.fetch(
        """
        SELECT DISTINCT UNNEST(specialties) as specialty
        FROM trader_profiles
        WHERE specialties IS NOT NULL AND ARRAY_LENGTH(specialties, 1) > 0
        ORDER BY specialty
        """
    )
    
    return [row["specialty"] for row in rows]


@router.get("/analytics", response_model=TraderAnalytics)
async def get_trader_analytics(
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get analytics for current trader
    """
    user = get_current_user(request)
    user_id = user["id"]

    # Check if user is a trader
    account_type = await db.fetchval(
        "SELECT account_type FROM users WHERE id = $1",
        user_id
    )

    if account_type != "trader":
        raise HTTPException(status_code=403, detail="Only traders can view analytics")

    # Get subscriber counts by tier
    rows = await db.fetch(
        """
        SELECT subscription_type, COUNT(*) as count
        FROM trader_subscriptions
        WHERE trader_id = $1 AND is_active = TRUE
        GROUP BY subscription_type
        """,
        user_id
    )
    
    counts = {
        'free': 0,
        'basic': 0,
        'premium': 0,
        'elite': 0
    }
    
    for row in rows:
        stype = row['subscription_type']
        if stype in counts:
            counts[stype] = row['count']

    # Get trader prices
    profile = await db.fetchrow(
        "SELECT * FROM trader_profiles WHERE user_id = $1",
        user_id
    )
    
    if not profile:
        raise HTTPException(status_code=404, detail="Trader profile not found")

    # Calculate earnings
    # Assuming monthly cycle
    earnings = Decimal(0)
    if profile['tier_basic_price']:
        earnings += counts['basic'] * profile['tier_basic_price']
    if profile['tier_premium_price']:
        earnings += counts['premium'] * profile['tier_premium_price']
    if profile['tier_elite_price']:
        earnings += counts['elite'] * profile['tier_elite_price']
    
    # Total followers (active free + active paid)
    total_followers = sum(counts.values())
    
    # Active paid subscribers
    total_active_paid = counts['basic'] + counts['premium'] + counts['elite']

    # Mock views for now or query posts views
    # If we have post views in social_posts, we can sum them up for last 30 days
    views = await db.fetchval(
        """
        SELECT COALESCE(SUM(views_count), 0)
        FROM social_posts
        WHERE user_id = $1 AND created_at > NOW() - INTERVAL '30 days'
        """,
        user_id
    )

    return TraderAnalytics(
        total_active_subscribers=total_active_paid,
        active_basic_subscribers=counts['basic'],
        active_premium_subscribers=counts['premium'],
        active_elite_subscribers=counts['elite'],
        monthly_earnings_estimated=earnings,
        total_followers=total_followers,
        views_last_30_days=views or 0
    )


@router.get("/{trader_id}", response_model=TraderPublicProfile)
async def get_trader_profile(
    trader_id: str,
    request: Request,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Get public trader profile
    """
    # Check if trader exists
    row = await db.fetchrow(
        """
        SELECT 
            tp.*, 
            up.username, up.avatar_url, up.header_image_url, 
            up.location, up.website_url, up.twitter_url, up.youtube_url, up.twitch_url,
            u.created_at as trader_since
        FROM trader_profiles tp
        JOIN users u ON tp.user_id = u.id
        JOIN user_profiles up ON tp.user_id = up.user_id
        WHERE tp.user_id = $1
        """, 
        trader_id
    )
    
    if not row:
        raise HTTPException(status_code=404, detail="Trader not found")

    result = dict(row)
    result["id"] = result["user_id"]
    
    # Check subscription status if user logged in
    try:
        user = get_current_user(request)
        sub = await db.fetchrow(
            """
            SELECT * FROM trader_subscriptions 
            WHERE subscriber_id = $1 AND trader_id = $2 AND is_active = TRUE
            """,
            user["id"], trader_id
        )
        result["is_subscribed"] = sub is not None
    except:
        result["is_subscribed"] = False

    return result


@router.get("/", response_model=List[TraderPublicProfile])
async def browse_traders(
    request: Request,
    specialty: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    db: asyncpg.Connection = Depends(get_db)
):
    """
    Browse and search traders
    """
    params = []
    where_clauses = ["u.account_type = 'trader'"]
    
    if specialty:
        params.append(specialty)
        where_clauses.append(f"$1 = ANY(tp.specialties)")
    
    if search:
        param_idx = len(params) + 1
        params.append(f"%{search}%")
        where_clauses.append(f"(up.username ILIKE ${param_idx} OR tp.bio ILIKE ${param_idx})")

    # Pagination
    limit_idx = len(params) + 1
    params.append(limit)
    offset_idx = len(params) + 1
    params.append(offset)

    query = f"""
        SELECT 
            tp.*, 
            up.username, up.avatar_url, up.header_image_url, 
            up.location, up.website_url, up.twitter_url, up.youtube_url, up.twitch_url,
            u.created_at as trader_since
        FROM trader_profiles tp
        JOIN users u ON tp.user_id = u.id
        JOIN user_profiles up ON tp.user_id = up.user_id
        WHERE {' AND '.join(where_clauses)}
        ORDER BY tp.total_followers DESC
        LIMIT ${limit_idx} OFFSET ${offset_idx}
    """
    
    rows = await db.fetch(query, *params)
    
    results = []
    for row in rows:
        d = dict(row)
        d["id"] = d["user_id"]
        d["is_subscribed"] = False # List view doesn't check sub status per item for perf
        results.append(d)
        
    return results
