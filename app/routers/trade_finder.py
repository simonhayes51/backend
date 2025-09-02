# app/routers/trade_finder.py
from __future__ import annotations

from fastapi import APIRouter, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional, Dict, Any, List
import json
import os

from app.services.trade_finder import find_deals, cache_key_from_filters

router = APIRouter()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

async def explain_deal_rule_based(d: dict) -> str:
    parts = []
    if d.get("margin_pct", 0) >= 12:
        parts.append("large discount vs baseline")
    elif d.get("margin_pct", 0) >= 8:
        parts.append("solid discount vs baseline")
    if "Undercut" in d.get("tags", []):
        parts.append("currently undercutting typical listings")
    if "Safe Floor" in d.get("tags", []):
        parts.append("near 7d floor (low downside risk)")
    if "Panic Dip" in d.get("tags", []):
        parts.append("short-term dip suggests bounce potential")
    if d.get("vol_score", 0) >= 0.1:
        parts.append("high volatility (fast flips possible)")
    msg = ", ".join(parts) or "priced below conservative sell target"
    return (
        f"{d['name']} {d['rating']} is priced at {d['current_price']:,}c vs a conservative sell target of "
        f"{d['expected_sell']:,}c (net {d['est_profit_after_tax']:,}c profit). Signals: {msg}."
    )

async def explain_deal_ai(d: dict) -> str:
    if not OPENAI_API_KEY:
        return await explain_deal_rule_based(d)
    import aiohttp
    sys_prompt = (
        "You are a FUT trading analyst. Explain in one short paragraph why this looks like a good flip. "
        "Be concrete and avoid hype. Use British English and include numbers."
    )
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    payload = {
        "model": "gpt-4o-mini",
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": json.dumps(d)},
        ],
        "temperature": 0.2,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post("https://api.openai.com/v1/chat/completions", headers=headers, json=payload) as r:
            if r.status != 200:
                return await explain_deal_rule_based(d)
            data = await r.json()
            try:
                return data["choices"][0]["message"]["content"].strip()
            except Exception:
                return await explain_deal_rule_based(d)

@router.get("/api/trade-finder")
async def api_trade_finder(
    request: Request,
    platform: str = Query("console", pattern="^(console|pc)$"),
    timeframe: int = Query(24, ge=4, le=24),
    budget_max: int = Query(150000, ge=0),
    min_profit: int = Query(1500, ge=0),
    min_margin_pct: float = Query(8.0, ge=0.0),
    rating_min: int = Query(75, ge=40, le=99),
    rating_max: int = Query(93, ge=40, le=99),
    leagues: Optional[str] = None,
    nations: Optional[str] = None,
    positions: Optional[str] = None,
    limit_players: int = Query(400, ge=50, le=1000),
    topn: int = Query(50, ge=1, le=100),
):
    pool = request.app.state.pool
    includes: Dict[str, List[str]] = {
        "leagues": [x.strip() for x in leagues.split(",") if x.strip()] if leagues else [],
        "nations": [x.strip() for x in nations.split(",") if x.strip()] if nations else [],
        "positions": [x.strip() for x in positions.split(",") if x.strip()] if positions else [],
    }
    filters = dict(platform=platform, timeframe_hours=timeframe, budget_max=budget_max, min_profit=min_profit,
                   min_margin_pct=min_margin_pct, rating_min=rating_min, rating_max=rating_max,
                   includes=includes, limit_players=limit_players, topn=topn)

    # cache lookup
    ck = cache_key_from_filters(filters)
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT payload FROM trade_finder_cache WHERE cache_key=$1 AND created_at > NOW() - INTERVAL '3 minutes'", ck)
            if row:
                return JSONResponse(row["payload"])
    except Exception:
        pass

    deals = await find_deals(pool, **filters)

    try:
        async with pool.acquire() as conn:
            await conn.execute("INSERT INTO trade_finder_cache(cache_key, payload) VALUES($1,$2) ON CONFLICT (cache_key) DO UPDATE SET payload=EXCLUDED.payload, created_at=NOW()", ck, json.dumps(deals))
    except Exception:
        pass

    return JSONResponse(deals)

class InsightIn(BaseModel):
    deal: Dict[str, Any]

@router.post("/api/trade-insight")
async def api_trade_insight(request: Request, payload: InsightIn):
    d = {k: payload.deal.get(k) for k in [
        "name","rating","league","nation","current_price","expected_sell","est_profit_after_tax",
        "margin_pct","tags","vol_score","change_pct_window","timeframe_hours"
    ]}
    text = await explain_deal_ai(d)
    return {"explanation": text}
