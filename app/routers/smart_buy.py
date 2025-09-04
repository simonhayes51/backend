# app/routers/smart_buy.py
from fastapi import APIRouter, Query, Request, HTTPException, Depends
from typing import Optional, List, Dict, Any, Literal
import asyncio
import logging
import json
import time
import math
import statistics
from datetime import datetime, timedelta, timezone
from collections import defaultdict

# Import your existing services
from app.services.price_history import get_price_history
from app.services.prices import get_player_price
from app.services.deal_confidence import compute_deal_confidence

router = APIRouter()

# Market states for smart buy suggestions
MARKET_STATES = {
    "NORMAL": "normal",
    "PRE_CRASH": "pre_crash", 
    "CRASH_ACTIVE": "crash_active",
    "RECOVERY": "recovery",
    "PROMO_HYPE": "promo_hype"
}

# Buy suggestion categories
BUY_CATEGORIES = {
    "CRASH_ANTICIPATION": "crash_anticipation",
    "RECOVERY_PLAYS": "recovery_plays", 
    "TREND_MOMENTUM": "trend_momentum",
    "VALUE_ARBITRAGE": "value_arbitrage"
}

# Cache for market analysis (5 minute TTL)
_market_cache = {"data": None, "timestamp": 0}
_suggestions_cache = {}

def get_current_user(request: Request) -> str:
    """Extract user_id from session"""
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# ----------------- HELPER FUNCTIONS -----------------

def _percentile(values: List[int], pct: float) -> int:
    """Calculate percentile of a list of values"""
    if not values:
        return 0
    xs = sorted(values)
    k = (len(xs) - 1) * (pct / 100.0)
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return xs[int(k)]
    d0 = xs[f] * (c - k)
    d1 = xs[c] * (k - f)
    return int(d0 + d1)

def _extract_prices_from_history(hist_data: Dict) -> List[int]:
    """Extract price values from history data"""
    points = hist_data.get("points", [])
    prices = []
    for point in points:
        price = point.get("price")
        if isinstance(price, (int, float)) and price > 0:
            prices.append(int(price))
    return prices

def _calculate_volatility(prices: List[int]) -> float:
    """Calculate price volatility (coefficient of variation)"""
    if len(prices) < 2:
        return 0.0
    try:
        mean = statistics.mean(prices)
        stdev = statistics.stdev(prices)
        return (stdev / mean) if mean > 0 else 0.0
    except:
        return 0.0

def _detect_trend(prices: List[int]) -> str:
    """Detect price trend: 'rising', 'falling', or 'stable'"""
    if len(prices) < 3:
        return "stable"
    
    # Linear regression slope
    n = len(prices)
    x_mean = (n - 1) / 2
    y_mean = sum(prices) / n
    
    numerator = sum((i - x_mean) * (prices[i] - y_mean) for i in range(n))
    denominator = sum((i - x_mean) ** 2 for i in range(n))
    
    if denominator == 0:
        return "stable"
    
    slope = numerator / denominator
    slope_pct = (slope / y_mean) * 100 if y_mean > 0 else 0
    
    if slope_pct > 2:
        return "rising"
    elif slope_pct < -2:
        return "falling"
    else:
        return "stable"

async def _analyze_market_state(platform: str) -> Dict[str, Any]:
    """Analyze current market conditions"""
    now = time.time()
    if _market_cache["data"] and (now - _market_cache["timestamp"]) < 300:  # 5 min cache
        return _market_cache["data"]
    
    try:
        # Sample key cards for market analysis
        sample_cards = [5, 1419, 4897, 20801, 1681]  # Mix of high-rated and popular cards
        
        analyses = []
        for card_id in sample_cards:
            try:
                hist = await get_price_history(card_id, platform, "today")
                prices = _extract_prices_from_history(hist)
                if len(prices) >= 10:
                    volatility = _calculate_volatility(prices)
                    trend = _detect_trend(prices[-12:])  # Last 3 hours
                    
                    # Price change over last 4 hours
                    recent_prices = prices[-16:] if len(prices) >= 16 else prices
                    change_pct = 0
                    if len(recent_prices) >= 2:
                        change_pct = ((recent_prices[-1] - recent_prices[0]) / recent_prices[0]) * 100
                    
                    analyses.append({
                        "volatility": volatility,
                        "trend": trend,
                        "change_pct": change_pct
                    })
            except:
                continue
        
        if not analyses:
            state = MARKET_STATES["NORMAL"]
            confidence = 50
        else:
            # Aggregate analysis
            avg_volatility = sum(a["volatility"] for a in analyses) / len(analyses)
            avg_change = sum(a["change_pct"] for a in analyses) / len(analyses)
            falling_count = sum(1 for a in analyses if a["trend"] == "falling")
            rising_count = sum(1 for a in analyses if a["trend"] == "rising")
            
            # Determine market state
            if avg_change < -8 and falling_count >= len(analyses) * 0.7:
                state = MARKET_STATES["CRASH_ACTIVE"]
                confidence = 85
            elif avg_change < -5 and avg_volatility > 0.15:
                state = MARKET_STATES["PRE_CRASH"]
                confidence = 75
            elif avg_change > 5 and rising_count >= len(analyses) * 0.6:
                state = MARKET_STATES["RECOVERY"]
                confidence = 80
            elif avg_volatility > 0.2:
                state = MARKET_STATES["PROMO_HYPE"]
                confidence = 70
            else:
                state = MARKET_STATES["NORMAL"]
                confidence = 90
        
        market_data = {
            "state": state,
            "confidence": confidence,
            "analysis": {
                "avg_volatility": avg_volatility if analyses else 0,
                "avg_change_4h": avg_change if analyses else 0,
                "sample_size": len(analyses)
            }
        }
        
        _market_cache["data"] = market_data
        _market_cache["timestamp"] = now
        
        return market_data
        
    except Exception as e:
        logging.error(f"Market analysis error: {e}")
        return {
            "state": MARKET_STATES["NORMAL"],
            "confidence": 50,
            "analysis": {}
        }

async def _generate_suggestion(pool, player_data: Dict, filters: Dict, market_state: str) -> Optional[Dict]:
    """Generate a smart buy suggestion for a single player"""
    card_id = player_data.get("card_id")
    if not card_id:
        return None
    
    try:
        platform = filters.get("platform", "ps")
        
        # Get price history and current price
        hist_data = await get_price_history(int(card_id), platform, "today")
        current_price = await get_player_price(int(card_id), platform)
        
        if not current_price or current_price <= 0:
            return None
            
        prices = _extract_prices_from_history(hist_data)
        if len(prices) < 6:  # Need minimum data
            return None
        
        # Budget filter
        budget = filters.get("budget", 100000)
        if current_price > budget:
            return None
        
        # Rating filter
        rating = player_data.get("rating", 0)
        if rating < filters.get("min_rating", 75) or rating > filters.get("max_rating", 95):
            return None
        
        # Calculate key metrics
        volatility = _calculate_volatility(prices)
        trend = _detect_trend(prices[-12:])
        
        # Target price calculation based on market state and recent history
        recent_high = max(prices[-24:]) if len(prices) >= 24 else max(prices)
        recent_low = min(prices[-24:]) if len(prices) >= 24 else min(prices)
        median_price = statistics.median(prices)
        
        # Adjust target based on market state
        if market_state == MARKET_STATES["CRASH_ACTIVE"]:
            target_price = int(median_price * 1.15)  # Expect 15% recovery
            category = BUY_CATEGORIES["RECOVERY_PLAYS"]
            risk_level = "low"
            reasoning = "Market crash detected - prime buying opportunity for recovery"
        elif market_state == MARKET_STATES["PRE_CRASH"]:
            return None  # Don't suggest during pre-crash
        elif market_state == MARKET_STATES["RECOVERY"]:
            target_price = int(median_price * 1.08)  # Modest gains
            category = BUY_CATEGORIES["TREND_MOMENTUM"] 
            risk_level = "medium"
            reasoning = "Market recovering - momentum play opportunity"
        elif trend == "falling" and current_price <= recent_low * 1.05:
            target_price = int(median_price * 1.06)
            category = BUY_CATEGORIES["VALUE_ARBITRAGE"]
            risk_level = "low"
            reasoning = "Price at recent low - value arbitrage opportunity"
        elif volatility > 0.1 and current_price < median_price:
            target_price = int(recent_high * 0.9)
            category = BUY_CATEGORIES["TREND_MOMENTUM"]
            risk_level = "medium"
            reasoning = "High volatility with current price below median"
        else:
            return None  # No clear opportunity
        
        # Calculate expected profit (after EA tax)
        expected_sell_after_tax = int(target_price * 0.95)  # 5% EA tax
        expected_profit = expected_sell_after_tax - current_price
        
        # Profit filters
        min_profit = filters.get("min_profit", 1000)
        if expected_profit < min_profit:
            return None
        
        # Risk assessment
        risk_factors = []
        if volatility > 0.15:
            risk_factors.append("High price volatility")
        if len(prices) < 20:
            risk_factors.append("Limited price data")
            
        # Time to profit estimation
        if market_state == MARKET_STATES["CRASH_ACTIVE"]:
            time_to_profit = "6-24h"
        elif volatility > 0.1:
            time_to_profit = "2-12h"
        else:
            time_to_profit = "12-48h"
        
        # Priority score (1-10)
        profit_score = min(5, expected_profit / 1000)  # Up to 5 points for profit
        market_score = 3 if market_state == MARKET_STATES["CRASH_ACTIVE"] else 1
        risk_score = 2 if risk_level == "low" else 1
        priority_score = min(10, int(profit_score + market_score + risk_score))
        
        # Confidence score
        data_quality = min(100, len(prices) * 2)  # More data = higher confidence
        market_confidence = 80 if market_state in [MARKET_STATES["CRASH_ACTIVE"], MARKET_STATES["RECOVERY"]] else 60
        confidence_score = int((data_quality + market_confidence) / 2)
        
        return {
            "card_id": str(card_id),
            "name": player_data.get("name", f"Player {card_id}"),
            "rating": rating,
            "position": player_data.get("position"),
            "version": player_data.get("version", "Base"),
            "image_url": player_data.get("image_url"),
            "current_price": current_price,
            "target_price": target_price,
            "expected_profit": expected_profit,
            "risk_level": risk_level,
            "category": category,
            "reason": reasoning,
            "time_to_profit": time_to_profit,
            "priority_score": priority_score,
            "confidence_score": confidence_score,
            "risk_factors": risk_factors
        }
        
    except Exception as e:
        logging.error(f"Suggestion generation error for card {card_id}: {e}")
        return None

# ----------------- API ENDPOINTS -----------------

@router.get("/smart-buy/suggestions")
async def get_smart_buy_suggestions(
    request: Request,
    budget: int = Query(100000, ge=1000, le=10000000),
    risk_tolerance: str = Query("moderate", regex="^(conservative|moderate|aggressive)$"),
    time_horizon: str = Query("short", regex="^(quick_flip|short|long_term)$"),
    platform: str = Query("ps", regex="^(ps|xbox|pc)$"),
    categories: Optional[str] = Query(None),
    min_rating: int = Query(75, ge=40, le=99),
    max_rating: int = Query(95, ge=40, le=99),
    exclude_positions: Optional[str] = Query(None),
    preferred_leagues: Optional[str] = Query(None),
    preferred_nations: Optional[str] = Query(None),
    user_id: str = Depends(get_current_user)
):
    """Generate smart buy suggestions based on user criteria"""
    
    try:
        pool = request.app.state.player_pool
        if not pool:
            raise HTTPException(500, "Player database not available")
        
        # Cache key for suggestions
        cache_key = f"{user_id}:{budget}:{risk_tolerance}:{platform}:{min_rating}:{max_rating}"
        now = time.time()
        
        if cache_key in _suggestions_cache:
            cached = _suggestions_cache[cache_key]
            if (now - cached["timestamp"]) < 300:  # 5 min cache
                return cached["data"]
        
        # Get market state
        market_analysis = await _analyze_market_state(platform)
        market_state = market_analysis["state"]
        
        # Build filters
        filters = {
            "budget": budget,
            "risk_tolerance": risk_tolerance,
            "time_horizon": time_horizon,
            "platform": platform,
            "min_rating": min_rating,
            "max_rating": max_rating,
            "min_profit": 1000 if risk_tolerance == "conservative" else 500,
            "categories": categories.split(",") if categories else []
        }
        
        # Query candidate players
        where_clauses = ["rating BETWEEN $1 AND $2"]
        params = [min_rating, max_rating]
        
        if preferred_leagues:
            leagues = [l.strip() for l in preferred_leagues.split(",")]
            where_clauses.append(f"LOWER(league) = ANY(${len(params)+1})")
            params.append([l.lower() for l in leagues])
        
        if preferred_nations:
            nations = [n.strip() for n in preferred_nations.split(",")]
            where_clauses.append(f"LOWER(nation) = ANY(${len(params)+1})")
            params.append([n.lower() for n in nations])
        
        if exclude_positions:
            positions = [p.strip().upper() for p in exclude_positions.split(",")]
            where_clauses.append(f"UPPER(position) != ALL(${len(params)+1})")
            params.append(positions)
        
        # Limit based on risk tolerance
        limit = 50 if risk_tolerance == "conservative" else 100
        
        sql = f"""
            SELECT card_id, name, rating, position, version, image_url, club, league, nation
            FROM fut_players
            WHERE {' AND '.join(where_clauses)}
            AND image_url IS NOT NULL
            ORDER BY rating DESC, name ASC
            LIMIT {limit}
        """
        
        async with pool.acquire() as conn:
            rows = await conn.fetch(sql, *params)
        
        if not rows:
            return {
                "suggestions": [],
                "market_state": market_state,
                "market_analysis": market_analysis["analysis"],
                "confidence_score": market_analysis["confidence"],
                "next_update": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            }
        
        # Generate suggestions concurrently
        semaphore = asyncio.Semaphore(10)  # Limit concurrent requests
        
        async def generate_with_semaphore(player_data):
            async with semaphore:
                return await _generate_suggestion(pool, dict(player_data), filters, market_state)
        
        suggestions_tasks = [generate_with_semaphore(row) for row in rows]
        suggestion_results = await asyncio.gather(*suggestions_tasks, return_exceptions=True)
        
        # Filter and sort suggestions
        suggestions = []
        for result in suggestion_results:
            if isinstance(result, dict) and result:
                suggestions.append(result)
        
        # Sort by priority score and expected profit
        suggestions.sort(key=lambda x: (x["priority_score"], x["expected_profit"]), reverse=True)
        
        # Limit final results
        max_suggestions = 20 if risk_tolerance == "conservative" else 30
        suggestions = suggestions[:max_suggestions]
        
        response_data = {
            "suggestions": suggestions,
            "market_state": market_state,
            "market_analysis": market_analysis["analysis"],
            "confidence_score": market_analysis["confidence"],
            "next_update": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        }
        
        # Cache the response
        _suggestions_cache[cache_key] = {
            "data": response_data,
            "timestamp": now
        }
        
        return response_data
        
    except Exception as e:
        logging.error(f"Smart buy suggestions error: {e}")
        raise HTTPException(500, f"Failed to generate suggestions: {str(e)}")

@router.get("/smart-buy/suggestion/{card_id}")
async def get_suggestion_detail(
    request: Request,
    card_id: str,
    platform: str = Query("ps"),
    user_id: str = Depends(get_current_user)
):
    """Get detailed analysis for a specific suggestion"""
    
    try:
        pool = request.app.state.pool
        
        # Get detailed analysis using your existing service
        analysis = await compute_deal_confidence(pool, int(card_id), platform)
        
        # Get price history
        hist_data = await get_price_history(int(card_id), platform, "today")
        week_data = await get_price_history(int(card_id), platform, "week")
        
        # Get current price
        current_price = await get_player_price(int(card_id), platform)
        
        # Calculate profit scenarios
        prices = _extract_prices_from_history(hist_data)
        if prices:
            recent_high = max(prices[-24:]) if len(prices) >= 24 else max(prices)
            recent_low = min(prices[-24:]) if len(prices) >= 24 else min(prices)
            median = statistics.median(prices)
            
            profit_scenarios = {
                "conservative": {
                    "target": int(median * 1.03),
                    "profit": int(median * 1.03 * 0.95) - current_price if current_price else 0,
                    "probability": 80
                },
                "moderate": {
                    "target": int(median * 1.06),
                    "profit": int(median * 1.06 * 0.95) - current_price if current_price else 0,
                    "probability": 60
                },
                "optimistic": {
                    "target": int(recent_high * 0.95),
                    "profit": int(recent_high * 0.95 * 0.95) - current_price if current_price else 0,
                    "probability": 30
                }
            }
        else:
            profit_scenarios = {}
        
        # Risk factors
        risk_factors = []
        if analysis.get("components", {}).get("volRisk", 0) > 0.15:
            risk_factors.append("High price volatility")
        if len(prices) < 20:
            risk_factors.append("Limited price history")
        if current_price and prices and current_price > max(prices) * 1.1:
            risk_factors.append("Price significantly above recent range")
        
        return {
            "card_id": card_id,
            "analysis": analysis,
            "price_history": hist_data.get("points", []),
            "week_history": week_data.get("points", []),
            "current_price": current_price,
            "profit_scenarios": profit_scenarios,
            "risk_factors": risk_factors,
            "similar_cards": []  # Could implement later
        }
        
    except Exception as e:
        logging.error(f"Suggestion detail error: {e}")
        raise HTTPException(500, f"Failed to get suggestion details: {str(e)}")

@router.get("/smart-buy/market-intelligence")
async def get_market_intelligence(platform: str = Query("ps")):
    """Get current market intelligence and upcoming events"""
    
    try:
        # Get market state
        market_analysis = await _analyze_market_state(platform)
        
        # Mock upcoming events (you can enhance this with real data)
        upcoming_events = [
            {
                "name": "Weekend League Rewards",
                "type": "rewards",
                "estimated_time": "2024-01-15T08:00:00Z",
                "impact": "medium",
                "description": "Increased supply expected"
            },
            {
                "name": "TOTW Release",
                "type": "promo",
                "estimated_time": "2024-01-17T18:00:00Z", 
                "impact": "high",
                "description": "Market hype around new cards"
            }
        ]
        
        # Mock whale activity (you can implement real tracking)
        whale_activity = [
            {
                "player_name": "Kylian Mbappe",
                "activity_type": "large_purchase",
                "estimated_volume": 50,
                "price_impact": "+12%",
                "timestamp": "2024-01-14T15:30:00Z"
            }
        ]
        
        # Mock meta shifts
        meta_shifts = [
            {
                "category": "CB",
                "trend": "rising",
                "reason": "New formation popularity",
                "impact_rating": 8
            }
        ]
        
        # Calculate crash probability based on market indicators
        crash_indicators = market_analysis.get("analysis", {})
        volatility = crash_indicators.get("avg_volatility", 0)
        price_change = crash_indicators.get("avg_change_4h", 0)
        
        crash_probability = 0
        if price_change < -5:
            crash_probability += 30
        if volatility > 0.15:
            crash_probability += 20
        if market_analysis["state"] == MARKET_STATES["PRE_CRASH"]:
            crash_probability = 75
        elif market_analysis["state"] == MARKET_STATES["CRASH_ACTIVE"]:
            crash_probability = 95
        
        crash_probability = min(100, crash_probability)
        
        # Recovery indicators
        recovery_indicators = {
            "oversold_cards": 15 if crash_probability > 70 else 5,
            "buying_volume": "increasing" if market_analysis["state"] == MARKET_STATES["RECOVERY"] else "stable",
            "sentiment_score": 30 if crash_probability > 50 else 70
        }
        
        return {
            "current_state": market_analysis["state"],
            "current_state_confidence": market_analysis["confidence"],
            "upcoming_events": upcoming_events,
            "crash_probability": crash_probability,
            "recovery_indicators": recovery_indicators,
            "whale_activity": whale_activity,
            "meta_shifts": meta_shifts
        }
        
    except Exception as e:
        logging.error(f"Market intelligence error: {e}")
        raise HTTPException(500, f"Failed to get market intelligence: {str(e)}")

@router.post("/smart-buy/feedback")
async def submit_suggestion_feedback(
    request: Request,
    user_id: str = Depends(get_current_user)
):
    """Submit feedback on a suggestion to improve future recommendations"""
    
    try:
        data = await request.json()
        card_id = data.get("card_id")
        action = data.get("action")  # "bought", "ignored", "watchlisted"
        notes = data.get("notes", "")
        timestamp = data.get("timestamp", datetime.now(timezone.utc).isoformat())
        
        if not card_id or not action:
            raise HTTPException(400, "card_id and action are required")
        
        pool = request.app.state.pool
        
        # Store feedback for ML training and user analytics
        async with pool.acquire() as conn:
            await conn.execute("""
                INSERT INTO smart_buy_feedback 
                (user_id, card_id, action, notes, timestamp)
                VALUES ($1, $2, $3, $4, $5)
            """, user_id, str(card_id), action, notes, timestamp)
        
        return {"success": True, "message": "Feedback recorded successfully"}
        
    except Exception as e:
        logging.error(f"Feedback submission error: {e}")
        raise HTTPException(500, f"Failed to submit feedback: {str(e)}")

@router.get("/smart-buy/stats")
async def get_suggestion_stats(
    request: Request,
    user_id: str = Depends(get_current_user)
):
    """Get user's suggestion performance statistics"""
    
    try:
        pool = request.app.state.pool
        
        async with pool.acquire() as conn:
            # Get basic stats
            stats = await conn.fetchrow("""
                SELECT 
                    COUNT(*) as total_suggestions,
                    COUNT(CASE WHEN action = 'bought' THEN 1 END) as suggestions_taken,
                    COUNT(CASE WHEN action = 'bought' THEN 1 END) * 100.0 / 
                        NULLIF(COUNT(*), 0) as success_rate
                FROM smart_buy_feedback 
                WHERE user_id = $1
            """, user_id)
            
            # Get category performance
            category_stats = await conn.fetch("""
                SELECT 
                    notes as category,
                    COUNT(*) as count,
                    COUNT(CASE WHEN action = 'bought' THEN 1 END) as taken
                FROM smart_buy_feedback 
                WHERE user_id = $1 AND notes IS NOT NULL
                GROUP BY notes
            """, user_id)
            
            category_performance = {}
            for row in category_stats:
                category_performance[row["category"]] = {
                    "suggestions": row["count"],
                    "taken": row["taken"],
                    "rate": (row["taken"] / row["count"]) * 100 if row["count"] > 0 else 0
                }
        
        # Mock profit data (you can enhance this with real trade tracking)
        return {
            "total_suggestions": stats["total_suggestions"] or 0,
            "suggestions_taken": stats["suggestions_taken"] or 0,
            "success_rate": round(stats["success_rate"] or 0, 1),
            "avg_profit": 2500,  # Mock data
            "total_profit": 15000,  # Mock data
            "category_performance": category_performance
        }
        
    except Exception as e:
        logging.error(f"Stats retrieval error: {e}")
        return {
            "total_suggestions": 0,
            "suggestions_taken": 0,
            "success_rate": 0,
            "avg_profit": 0,
            "total_profit": 0,
            "category_performance": {}
        }
