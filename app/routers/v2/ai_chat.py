# app/routers/v2/ai_chat.py
#
# OpenAI-backed chat, orchestrating real tool calls into recommendation_
# engine_v2.py/trade_finder.py/track-record - never a free-form model
# answer. The model's only jobs are intent-parsing (which tool to call,
# with what arguments) and turning a tool's JSON result into plain
# language; every price/ROI/recommendation in a reply must trace back to
# a tool call. This is the same "never fabricate a number" discipline
# trading_math.py exists to enforce everywhere else in this codebase -
# one hallucinated price here would undo that.
#
# Gated Pro+ (see app/auth/entitlements.py): unlike the free ai_copilot
# keyword-matcher, every message here costs a real OpenAI call.
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.entitlements import compute_entitlements, require_feature
from app.routers.trade_finder import trade_finder
from app.routers.v2.recommendations import get_player_recommendation, track_record
from app.services.player_resolver import resolve_card

log = logging.getLogger("ai_chat")

router = APIRouter(prefix="/ai", tags=["v2-ai-chat"])

OPENAI_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
_RATE_LIMIT_PER_MINUTE = int(os.getenv("AI_CHAT_RATE_LIMIT_PER_MINUTE", "6"))

_redis = None
_redis_checked = False


async def _get_redis():
    """Same lazy-optional pattern as app/auth/api_keys.py - Redis is
    already a dependency, this just reuses it rather than adding a new
    rate-limit backend for one endpoint."""
    global _redis, _redis_checked
    if _redis_checked:
        return _redis
    _redis_checked = True
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    try:
        import redis.asyncio as aioredis  # type: ignore

        _redis = aioredis.from_url(url, decode_responses=True)
        await _redis.ping()
    except Exception as e:
        log.warning("REDIS_URL set but unusable for ai_chat rate limiting (%s)", e)
        _redis = None
    return _redis


_in_process_window: Dict[str, tuple[float, int]] = {}


async def _check_rate_limit(user_id: str) -> None:
    r = await _get_redis()
    if r is not None:
        try:
            bucket = f"rl:ai_chat:{user_id}:{int(time.time() // 60)}"
            n = await r.incr(bucket)
            if n == 1:
                await r.expire(bucket, 90)
            if n > _RATE_LIMIT_PER_MINUTE:
                raise HTTPException(429, "Too many chat messages - please wait a moment.")
            return
        except HTTPException:
            raise
        except Exception:
            pass  # Redis hiccup -> fall through to in-process limiter

    now = time.time()
    window_start, count = _in_process_window.get(user_id, (now, 0))
    if now - window_start >= 60:
        _in_process_window[user_id] = (now, 1)
        return
    if count >= _RATE_LIMIT_PER_MINUTE:
        raise HTTPException(429, "Too many chat messages - please wait a moment.")
    _in_process_window[user_id] = (window_start, count + 1)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(..., min_length=1)


TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "resolve_and_evaluate_card",
            "description": (
                "Resolve a card by name (optionally with a rating, e.g. 'Mbappe 92') and return "
                "its full BUY/HOLD/SELL/AVOID recommendation, confidence, and reasoning. Use this "
                "for any question about a specific player/card."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Player name, optionally with a rating, e.g. 'Mbappe 92'"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_trades_for_budget",
            "description": (
                "Find profitable trade candidates within a coin budget. Use this for questions like "
                "'who should I buy for 40k' or 'best trades under 100000'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "budget_max": {"type": "number", "description": "Maximum coins to spend, e.g. 40000 for '40k'"},
                    "budget_min": {"type": "number", "description": "Minimum coins to spend, if the user gave a range"},
                },
                "required": ["budget_max"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_track_record",
            "description": (
                "Get the real, historical hit-rate of the recommendation engine's strategies. Use "
                "this when asked how accurate/reliable the recommendations are."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

SYSTEM_PROMPT = (
    "You are a FUT (EA FC Ultimate Team) trading assistant. You have no market knowledge of your "
    "own - every price, ROI, confidence score, or recommendation you state MUST come from a tool "
    "call result. Never state a coin value, percentage, or BUY/SELL verdict that didn't come from a "
    "tool. If a tool returns a disambiguation list (multiple candidates), ask the user which one "
    "they meant instead of guessing. If no tool applies to the question, say so plainly rather than "
    "answering from general knowledge. Keep replies short and concrete."
)


async def _run_tool(request: Request, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    player_pool = getattr(request.app.state, "player_pool", None)
    if name == "resolve_and_evaluate_card":
        query = str(args.get("query") or "").strip()
        if not query or player_pool is None:
            return {"error": "No query provided"}
        async with player_pool.acquire() as conn:
            resolution = await resolve_card(conn, query)
        if resolution.card is None and resolution.candidates:
            return {
                "ambiguous": True,
                "candidates": [
                    {"card_id": c.card_id, "name": c.name, "rating": c.rating, "version": c.version}
                    for c in resolution.candidates
                ],
            }
        if resolution.card is None:
            return {"error": f"No card found matching '{query}'"}
        try:
            recommendation = await get_player_recommendation(resolution.card.card_id, request)
        except HTTPException as e:
            return {"error": e.detail, "card": {"card_id": resolution.card.card_id, "name": resolution.card.name, "rating": resolution.card.rating}}
        return {"card": {"card_id": resolution.card.card_id, "name": resolution.card.name, "rating": resolution.card.rating}, "recommendation": recommendation}

    if name == "find_trades_for_budget":
        budget_max = args.get("budget_max")
        budget_min = args.get("budget_min")
        if budget_max is None:
            return {"error": "budget_max is required"}
        result = await trade_finder(
            request,
            budget_min=budget_min,
            budget_max=budget_max,
            topn=5,
        )
        return result

    if name == "get_track_record":
        return await track_record(request)

    return {"error": f"Unknown tool: {name}"}


@router.post("/chat")
async def chat(body: ChatRequest, request: Request) -> Dict[str, Any]:
    await require_feature("ai_chat")(request)
    ent = await compute_entitlements(request)
    user_id = ent.get("user_id")
    if not user_id:
        raise HTTPException(401, "Not authenticated")
    await _check_rate_limit(str(user_id))

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {
            "reply": "AI chat isn't configured yet - ask an admin to set OPENAI_API_KEY.",
            "toolResults": [],
        }

    from openai import AsyncOpenAI  # imported lazily so a missing/unset key never breaks module import

    client = AsyncOpenAI(api_key=api_key)

    messages: List[Dict[str, Any]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend({"role": m.role, "content": m.content} for m in body.messages)

    tool_results: List[Dict[str, Any]] = []

    try:
        for _ in range(3):  # bounded tool-call loop - never let a model loop forever
            response = await client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
            )
            choice = response.choices[0]
            message = choice.message

            if not message.tool_calls:
                return {"reply": message.content or "", "toolResults": tool_results}

            messages.append({
                "role": "assistant",
                "content": message.content,
                "tool_calls": [tc.model_dump() for tc in message.tool_calls],
            })

            for tool_call in message.tool_calls:
                try:
                    args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await _run_tool(request, tool_call.function.name, args)
                tool_results.append({"tool": tool_call.function.name, "args": args, "result": result})
                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "content": json.dumps(result, default=str),
                })

        # Loop exhausted without a final text reply - surface the last
        # tool results rather than silently returning nothing.
        return {"reply": "Here's what I found:", "toolResults": tool_results}
    except Exception as e:
        log.exception("ai_chat: OpenAI call failed")
        return {"reply": f"The AI chat is temporarily unavailable ({type(e).__name__}). Please try again shortly.", "toolResults": tool_results}
