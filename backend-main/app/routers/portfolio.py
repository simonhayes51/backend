# app/routers/portfolio.py
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from typing import Dict, Any, List, Optional, Literal
from app.db import get_db
from app.auth.entitlements import compute_entitlements
import asyncpg
from datetime import datetime, timedelta

router = APIRouter(prefix="/api/ai", tags=["Portfolio"])

class OptimizePortfolioRequest(BaseModel):
    goal: Literal["aggressive", "balanced", "conservative"] = "balanced"

@router.post("/optimize-portfolio")
async def optimize_portfolio(
    req: Request,
    body: OptimizePortfolioRequest,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    Calculate Sharpe ratio, max drawdown, win rate from user's trades.
    Generate personalized recommendations based on goal.
    Return portfolio health score and diversification analysis.
    """
    try:
        ent = await compute_entitlements(req)
        user_id = ent.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")

        # Fetch user's trades
        trades = await db.fetch(
            """
            SELECT
                player, version, buy, sell, quantity,
                profit, timestamp, platform, tag
            FROM trades
            WHERE user_id = $1
            ORDER BY timestamp ASC
            """,
            user_id
        )

        if not trades:
            return {
                "ok": False,
                "message": "No trades found. Start trading to get portfolio insights!",
                "health_score": 0,
                "metrics": {},
                "recommendations": []
            }

        # Calculate metrics
        total_trades = len(trades)
        winning_trades = [t for t in trades if t["profit"] > 0]
        losing_trades = [t for t in trades if t["profit"] < 0]
        win_rate = len(winning_trades) / total_trades if total_trades > 0 else 0

        total_profit = sum(t["profit"] for t in trades)
        avg_profit = total_profit / total_trades if total_trades > 0 else 0

        # Calculate max drawdown
        cumulative = 0
        peak = 0
        max_drawdown = 0
        for t in trades:
            cumulative += t["profit"]
            if cumulative > peak:
                peak = cumulative
            drawdown = (peak - cumulative) / peak if peak > 0 else 0
            if drawdown > max_drawdown:
                max_drawdown = drawdown

        # Calculate Sharpe ratio (simplified)
        if len(trades) > 1:
            profits = [t["profit"] for t in trades]
            mean_profit = sum(profits) / len(profits)
            variance = sum((p - mean_profit) ** 2 for p in profits) / len(profits)
            std_dev = variance ** 0.5
            sharpe_ratio = (mean_profit / std_dev) if std_dev > 0 else 0
        else:
            sharpe_ratio = 0

        # Diversification analysis
        unique_players = len(set(t["player"] for t in trades))
        diversification_score = min(unique_players / 10, 1.0)  # 10+ players = max diversification

        # Portfolio health score (0-100)
        health_score = int(
            (win_rate * 40) +  # 40% weight on win rate
            (min(sharpe_ratio / 2, 1.0) * 30) +  # 30% weight on Sharpe ratio
            ((1 - max_drawdown) * 20) +  # 20% weight on low drawdown
            (diversification_score * 10)  # 10% weight on diversification
        )

        # Generate recommendations based on goal
        recommendations = []

        if body.goal == "aggressive":
            if win_rate < 0.6:
                recommendations.append({
                    "type": "strategy",
                    "title": "Improve Win Rate",
                    "description": f"Your current win rate is {win_rate*100:.1f}%. For aggressive growth, target 60%+ by focusing on high-confidence trades.",
                    "priority": "high"
                })
            if diversification_score < 0.5:
                recommendations.append({
                    "type": "diversification",
                    "title": "Expand Player Pool",
                    "description": f"You're trading only {unique_players} unique players. Diversify to 15+ players to reduce risk.",
                    "priority": "medium"
                })
            recommendations.append({
                "type": "strategy",
                "title": "Maximize High-Margin Trades",
                "description": "Focus on players with 10%+ profit margins. Use AI recommendations to find trending cards.",
                "priority": "high"
            })

        elif body.goal == "balanced":
            if max_drawdown > 0.2:
                recommendations.append({
                    "type": "risk",
                    "title": "Reduce Drawdown",
                    "description": f"Your max drawdown is {max_drawdown*100:.1f}%. Set stop-loss limits at 15% to protect capital.",
                    "priority": "high"
                })
            if win_rate < 0.55:
                recommendations.append({
                    "type": "strategy",
                    "title": "Target 55%+ Win Rate",
                    "description": f"Current win rate: {win_rate*100:.1f}%. Use trade finder to identify safer opportunities.",
                    "priority": "medium"
                })
            recommendations.append({
                "type": "diversification",
                "title": "Maintain Portfolio Balance",
                "description": f"Keep trading {unique_players if unique_players > 8 else '8-12'} different players to balance risk and reward.",
                "priority": "medium"
            })

        else:  # conservative
            if max_drawdown > 0.15:
                recommendations.append({
                    "type": "risk",
                    "title": "Minimize Drawdown",
                    "description": f"Your max drawdown is {max_drawdown*100:.1f}%. Target <10% by avoiding volatile cards.",
                    "priority": "high"
                })
            if diversification_score < 0.7:
                recommendations.append({
                    "type": "diversification",
                    "title": "Increase Diversification",
                    "description": f"Trade at least 15+ different players to minimize exposure to any single card.",
                    "priority": "high"
                })
            recommendations.append({
                "type": "strategy",
                "title": "Focus on Low-Risk Trades",
                "description": "Target 3-5% profit margins on high-volume, stable cards. Avoid hype-driven spikes.",
                "priority": "high"
            })

        # Always add general recommendations if applicable
        if len(losing_trades) > len(winning_trades):
            recommendations.append({
                "type": "strategy",
                "title": "Review Losing Patterns",
                "description": f"You have {len(losing_trades)} losing trades vs {len(winning_trades)} winners. Analyze what went wrong and adjust strategy.",
                "priority": "high"
            })

        return {
            "ok": True,
            "health_score": health_score,
            "metrics": {
                "total_trades": total_trades,
                "win_rate": round(win_rate, 3),
                "total_profit": total_profit,
                "avg_profit_per_trade": round(avg_profit, 2),
                "sharpe_ratio": round(sharpe_ratio, 3),
                "max_drawdown": round(max_drawdown, 3),
                "diversification_score": round(diversification_score, 3),
                "unique_players": unique_players
            },
            "recommendations": recommendations,
            "goal": body.goal
        }

    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"optimize-portfolio error: {e}")
        raise HTTPException(status_code=500, detail=f"Portfolio optimization failed: {str(e)}")
