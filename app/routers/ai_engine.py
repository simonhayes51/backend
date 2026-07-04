# app/routers/ai_engine.py
import asyncio
import statistics
from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Dict, Any, List
from pydantic import BaseModel
from app.db import get_db
from app.auth.entitlements import compute_entitlements
from app.services.price_history import get_price_history

router = APIRouter(prefix="/api/ai", tags=["AI Engine"])

class CopilotChatMessage(BaseModel):
    role: str
    content: str

class CopilotMessage(BaseModel):
    # Frontend (TradeCopilot.jsx) POSTs the full running chat transcript as
    # `messages: [{role, content}, ...]` plus a `context` object - it never
    # sends a single top-level `message` string, so that shape 422'd every
    # request before the handler ran.
    messages: List[CopilotChatMessage] = []
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
        
        # Keep only rows with a usable current price before doing any
        # network work for history.
        candidates = []
        for row in rows:
            if row["price_num"]:
                current_price = int(row["price_num"])
            elif row["price"] and str(row["price"]).isdigit():
                current_price = int(row["price"])
            else:
                continue  # Skip if no valid price
            candidates.append((row, current_price))

        if not candidates:
            return []

        # Real market history (futbin-derived sales, bucketed by
        # get_price_history) for each candidate. These are outbound network
        # calls to futbin per card, so fetch them concurrently but bounded -
        # same throttling pattern used for get_price_history() calls in
        # app/routers/trade_finder.py (asyncio.Semaphore + gather).
        sem = asyncio.Semaphore(8)

        async def _throttled_hist(card_id: int) -> Dict[str, Any]:
            async with sem:
                try:
                    return await get_price_history(card_id, platform, "week")
                except Exception:
                    return {"points": []}

        histories = await asyncio.gather(
            *(_throttled_hist(int(row["card_id"])) for row, _ in candidates)
        )

        results = []
        for (row, current_price), hist in zip(candidates, histories):
            points = (hist or {}).get("points") or []
            prices = [
                float(p["price"])
                for p in points
                if isinstance(p, dict) and p.get("price") is not None
            ]

            if not prices:
                # No real market signal for this card at all - skip it
                # rather than present a fabricated "good buy" score.
                continue

            # "median7" = real median sale price over the (up to 7-day)
            # history window returned by get_price_history(tf="week"),
            # not a guess derived from the current price.
            median7 = statistics.median(prices)

            # Same cheap/expensive-vs-median formula as before, just fed by
            # the real median instead of a fabricated one.
            cheap_pct = (current_price - median7) / median7 if median7 else 0.0

            # No dedicated trade-count/volume field is exposed by
            # get_price_history() (futbin has no public sales-volume API,
            # only the bucketed price series), and fut_candles.volume is a
            # tick-count from the older fut.gg-fed pipeline keyed off a
            # different id space. So use the number of real bucketed price
            # points returned for this card over the window as a genuine
            # liquidity/activity proxy - more tracked data points means the
            # card is more actively traded/tracked, not a hash of card_id.
            vol24 = len(prices)

            # Same risk thresholds as before, now driven by real cheap_pct.
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
                "median7": int(round(median7)),
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

        # Frontend sends the full transcript as `messages`; pull the latest
        # user turn's text and feed it into the existing keyword-matching
        # logic below exactly where it used to use `body.message`.
        user_messages = [m for m in body.messages if m.role == "user"]
        if not user_messages:
            raise HTTPException(status_code=422, detail="No user message provided in 'messages'")

        message = user_messages[-1].content.lower().strip()

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

        # Live price/value questions - this bot only has static advice text, no
        # live pricing lookup, so "how much are TOTW cards" or "what's X worth"
        # must NOT fall through to the generic investment-budget branch below
        # (it used to, since that branch's "how much" keyword matches almost
        # any price question too, making unrelated questions return identical
        # canned text). Answer honestly instead of guessing a number.
        if any(keyword in message for keyword in [
            "how much is", "how much are", "how much will", "price of", "current price",
            "cost of", "worth now", "value of", "totw price", "rise to", "going up", "going to rise",
        ]):
            response = "I can't pull live prices in this chat - I only give trading strategy advice, not real-time card values.\n\n"
            response += "For actual current prices and trends, use:\n"
            response += "• **Player Search** to look up any card's live price\n"
            response += "• **Trending** for what's rising/falling right now\n"
            response += "• **Best Buys** or **Smart Buy** for suggested opportunities\n\n"
            response += "Ask me about strategy, timing, risk, or your own performance instead!"

        # Investment questions
        elif any(keyword in message for keyword in ["how much should i invest", "how much per trade", "invest", "budget", "coins"]):
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
