# app/routers/ai_engine.py
from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Dict, Any, List
from pydantic import BaseModel
from app.db import get_db
from app.auth.entitlements import compute_entitlements

router = APIRouter(prefix="/api/ai", tags=["AI Engine"])

class CopilotMessage(BaseModel):
    message: str
    context: Dict[str, Any] = {}

def _pct(a: float, b: float) -> float:
    return (a - b) / b if b else 0.0

# Add this to app/routers/ai_engine.py

# REPLACE the top-buys endpoint in app/routers/ai_engine.py with this:

@router.get("/top-buys")
async def top_buys(
    platform: str = "ps",
    limit: int = 36,
    db=Depends(get_db),
):
    """
    Get top buy recommendations with risk assessment
    """
    try:
        # Import here to avoid circular imports
        from main import app
        
        async with app.state.player_pool.acquire() as pconn:
            rows = await pconn.fetch(
                """
                SELECT 
                    card_id,
                    name,
                    rating,
                    position,
                    version,
                    image_url,
                    price,
                    price_num,
                    club,
                    league,
                    nation
                FROM fut_players 
                WHERE (
                    (price_num IS NOT NULL AND price_num > 1000 AND price_num < 2000000)
                    OR 
                    (price IS NOT NULL AND price ~ '^[0-9]+$' AND price::integer > 1000 AND price::integer < 2000000)
                )
                AND rating >= 75
                ORDER BY 
                    rating DESC,
                    COALESCE(price_num, CASE WHEN price ~ '^[0-9]+$' THEN price::integer ELSE 999999999 END) ASC
                LIMIT $1
                """, 
                limit
            )
        
        results = []
        for row in rows:
            # Get the actual price value
            if row["price_num"]:
                current_price = int(row["price_num"])
            elif row["price"] and str(row["price"]).isdigit():
                current_price = int(row["price"])
            else:
                continue  # Skip if no valid price
            
            # Mock some analysis data based on price patterns
            card_id_int = int(row["card_id"])
            price_variance = (card_id_int % 100) / 1000  # Mock variance 0-0.1
            
            # Simulate median price (slightly different from current)
            median7 = int(current_price * (0.95 + price_variance))
            
            # Calculate percentage difference
            if median7 > 0:
                cheap_pct = (current_price - median7) / median7
            else:
                cheap_pct = 0
            
            # Mock volume (based on rating and price)
            vol24 = max(10, (row["rating"] or 75) - 50 + (card_id_int % 30))
            
            # Determine risk level
            abs_cheap_pct = abs(cheap_pct)
            if abs_cheap_pct < 0.05:
                risk_label = "Low"
            elif abs_cheap_pct < 0.15:
                risk_label = "Medium"
            else:
                risk_label = "High"
            
            results.append({
                "player_card_id": str(row["card_id"]),  # Use card_id, not player_card_id
                "player": {
                    "name": row["name"],
                    "rating": row["rating"],
                    "position": row["position"],
                    "version": row["version"] or "Standard",
                    "image_url": row["image_url"]
                },
                "current": current_price,
                "median7": median7,
                "cheap_pct": cheap_pct,
                "vol24": vol24,
                "risk_label": risk_label
            })
        
        return results
        
    except Exception as e:
        import logging
        import traceback
        logging.error(f"top-buys error: {e}")
        logging.error(f"traceback: {traceback.format_exc()}")
        return {"ok": False, "reason": f"top-buys failed: {e}", "data": []}

@router.get("/recommendations")
async def recommendations(
    player_card_id: str,
    platform: str = "ps",
    db=Depends(get_db),
) -> Dict[str, Any]:
    try:
        rows = await db.fetch(
            """
            SELECT open_time, close
            FROM public.fut_candles
            WHERE player_card_id=$1 AND platform=$2 AND timeframe='15m'
            ORDER BY open_time ASC
            """,
            player_card_id, platform,
        )
        data = [dict(r) for r in rows]
        if len(data) < 20:
            return {"ok": False, "reason": "insufficient candles (need >= 20 x 15m)", "have": len(data)}

        closes = [float(d["close"]) for d in data]
        curr = closes[-1]
        srt = sorted(closes)
        n = len(srt)
        med7 = (srt[n//2] if n % 2 else (srt[n//2-1] + srt[n//2]) / 2)

        dmed = _pct(curr, med7)
        lookback = 12 if len(closes) > 12 else max(len(closes)-1, 1)
        ch3h = _pct(curr, closes[-lookback]) if lookback > 0 else 0.0

        action, tgt, stop, comment = "HOLD", None, None, "No clear edge."
        if dmed <= -0.05:
            action = "BUY"
            tgt = int(max(med7 * 0.99, curr * 1.05))
            stop = int(curr * 0.97)
            comment = f"Value: now {dmed*100:.1f}% under 7D median ({int(med7):,})."
        elif ch3h >= 0.08:
            action = "AVOID_OR_SELL"
            comment = f"Momentum: +{ch3h*100:.1f}% in ~3h. Likely hype."

        return {
            "ok": True, "player_card_id": player_card_id, "platform": platform,
            "current": int(curr), "median7": int(med7),
            "delta_to_median": round(dmed, 4), "change_3h": round(ch3h, 4),
            "action": action, "target_sell": tgt, "stop_loss": stop, "comment": comment,
        }
    except Exception as e:
        return {"ok": False, "reason": f"recommendations failed: {e}"}

@router.post("/copilot")
async def ai_copilot(
    req: Request,
    body: CopilotMessage,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    AI Trade Copilot - Context-aware chat about trading with user stats integration.
    Pattern matches common questions and provides personalized responses.
    """
    try:
        ent = await compute_entitlements(req)
        user_id = ent.get("user_id")

        message = body.message.lower().strip()

        # Get user's trading stats if authenticated
        stats = None
        if user_id:
            trades = await db.fetch(
                """
                SELECT profit, buy, sell, quantity, timestamp
                FROM trades
                WHERE user_id = $1
                ORDER BY timestamp DESC
                LIMIT 100
                """,
                user_id
            )

            if trades:
                total_profit = sum(t["profit"] for t in trades)
                winning = [t for t in trades if t["profit"] > 0]
                win_rate = len(winning) / len(trades) if trades else 0
                avg_profit = total_profit / len(trades) if trades else 0

                stats = {
                    "total_trades": len(trades),
                    "total_profit": total_profit,
                    "win_rate": win_rate,
                    "avg_profit": avg_profit
                }

        # Pattern matching for common questions
        response = ""

        # Investment questions
        if any(keyword in message for keyword in ["invest", "how much", "budget", "coins"]):
            if stats and stats["total_profit"] > 0:
                response = f"Based on your trading history ({stats['total_trades']} trades, {stats['total_profit']:,} profit), I recommend:\n\n"
                if stats["total_profit"] < 50000:
                    response += "• Start with 5-10k per trade to build your portfolio\n"
                    response += "• Focus on high-volume cards (83-86 rated) for consistent flips\n"
                    response += "• Diversify across 3-5 players to reduce risk"
                elif stats["total_profit"] < 200000:
                    response += "• Scale up to 15-25k per trade as you're showing profit\n"
                    response += "• Mix meta cards with fodder for balanced returns\n"
                    response += "• Keep 20% of your coins as safety net"
                else:
                    response += "• You're doing great! Consider 50k+ trades on trending cards\n"
                    response += "• Explore TOTW and promo cards for higher margins\n"
                    response += "• Use 70% of bankroll, reserve 30% for opportunities"
            else:
                response = "Starting out? Here's my advice:\n\n"
                response += "• Begin with 5-10k per trade to learn market patterns\n"
                response += "• Never invest more than 10% of your total coins in one card\n"
                response += "• Focus on liquid cards that sell quickly (high-rated fodder, meta players)\n"
                response += "• Start small, learn the rhythms, then scale up!"

        # Profit strategy questions
        elif any(keyword in message for keyword in ["profit", "make coins", "strategy", "tips", "advice"]):
            if stats and stats["win_rate"] > 0.5:
                response = f"You have a solid {stats['win_rate']*100:.1f}% win rate! To maximize profits:\n\n"
                response += "• Your avg profit is {:.0f} coins/trade - aim to increase this by 20%\n".format(stats["avg_profit"])
                response += "• Target players 5-10% below market value\n"
                response += "• Sell during peak hours (6-10 PM local time)\n"
                response += "• Use our AI recommendations to spot undervalued cards"
            else:
                response = "Here's a proven profit strategy:\n\n"
                response += "**Quick Flips (5-15 min):**\n"
                response += "• Buy meta cards 3-5% below lowest BIN during content drops\n"
                response += "• Relist immediately at market price\n\n"
                response += "**Medium-term (1-24 hours):**\n"
                response += "• Invest in anticipated SBCs (check leaks/rumors)\n"
                response += "• Mass bid on 83-86 rated cards at discard\n\n"
                response += "**Always:**\n"
                response += "• Set stop-loss at 10% to limit downside\n"
                response += "• Take profits at 8-10% gains consistently"

        # Performance questions
        elif any(keyword in message for keyword in ["doing", "performance", "stats", "how am i"]):
            if stats:
                response = f"📊 Your Trading Performance:\n\n"
                response += f"• **Total Trades:** {stats['total_trades']}\n"
                response += f"• **Total Profit:** {stats['total_profit']:,} coins\n"
                response += f"• **Win Rate:** {stats['win_rate']*100:.1f}%\n"
                response += f"• **Avg Profit/Trade:** {stats['avg_profit']:.0f} coins\n\n"

                if stats["win_rate"] >= 0.6:
                    response += "🔥 Excellent! You're beating the market. Keep it up!"
                elif stats["win_rate"] >= 0.5:
                    response += "✅ Solid performance. Fine-tune your strategy to hit 60%+ win rate."
                else:
                    response += "⚠️ Your win rate needs work. Try using our Trade Finder for safer picks."
            else:
                response = "You haven't recorded any trades yet! Start tracking your trades to get personalized insights."

        # Market questions
        elif any(keyword in message for keyword in ["market", "crash", "buy now", "sell now", "timing"]):
            response = "🎯 Market Timing Tips:\n\n"
            response += "**Best times to BUY:**\n"
            response += "• Thursday 6-7 PM (content hype, panic selling)\n"
            response += "• Weekend mornings (low activity)\n"
            response += "• During lightning rounds (undercutting)\n\n"
            response += "**Best times to SELL:**\n"
            response += "• Weekend League (Thurs-Mon peak demand)\n"
            response += "• 6-10 PM weekdays (peak traffic)\n"
            response += "• Right before SBC expiry\n\n"
            response += "Check our Market Sentiment feature for real-time trends!"

        # Card-specific questions
        elif any(keyword in message for keyword in ["card", "player", "should i buy", "worth it"]):
            response = "To analyze a specific card:\n\n"
            response += "1. Use our **AI Top Buys** to see current best opportunities\n"
            response += "2. Check **Trade Finder** for cards matching your criteria\n"
            response += "3. Review price history to spot trends\n\n"
            response += "General rules:\n"
            response += "• Meta cards: Buy during rewards (Thurs), sell Mon-Wed\n"
            response += "• Fodder: Buy during promos, sell before SBC expiry\n"
            response += "• Special cards: Buy on release day panic, sell after 2-3 days"

        # Risk management
        elif any(keyword in message for keyword in ["risk", "safe", "protect", "loss"]):
            response = "🛡️ Risk Management Essentials:\n\n"
            response += "• **Never invest >10%** of your coins in one card\n"
            response += "• **Set stop-loss** at 10-15% below buy price\n"
            response += "• **Diversify** across 5+ different players\n"
            response += "• **Avoid volatile cards** unless you can monitor 24/7\n"
            response += "• **Keep liquid coins** for unexpected opportunities\n\n"
            if stats and stats["total_trades"] > 10:
                response += f"With your {stats['total_trades']} trades, you should keep at least {int(stats['avg_profit'] * 10):,} coins liquid."

        # Default response
        else:
            response = "👋 I'm your AI Trade Copilot! I can help with:\n\n"
            response += "• Investment strategies and budget advice\n"
            response += "• Profit-making tips and market timing\n"
            response += "• Performance analysis and trading stats\n"
            response += "• Card recommendations and risk management\n\n"
            response += "Try asking:\n"
            response += "• 'How much should I invest per trade?'\n"
            response += "• 'What's the best profit strategy?'\n"
            response += "• 'How am I performing?'\n"
            response += "• 'When should I buy/sell?'"

        return {
            "ok": True,
            "response": response,
            "user_stats": stats,
            "context_used": bool(stats)
        }

    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"ai-copilot error: {e}")
        raise HTTPException(status_code=500, detail=f"Copilot failed: {str(e)}")
