import os
import json
import asyncpg
import aiohttp
import csv
import io
import logging
import time
import secrets
import jwt
from types import SimpleNamespace
from urllib.parse import urlencode

from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Optional, Dict, Any

logging.basicConfig(level=logging.INFO)

load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL", DATABASE_URL)
WATCHLIST_DATABASE_URL = os.getenv("WATCHLIST_DATABASE_URL", DATABASE_URL)

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://frontend-production-ab5e.up.railway.app")
PORT = int(os.getenv("PORT", 8000))

JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "dev-secret-change-me")
JWT_ISSUER = os.getenv("JWT_ISSUER", "fut-dashboard")
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "2592000"))
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")
OAUTH_STATE: Dict[str, Dict[str, Any]] = {}

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# --------- MODELS ---------
class TradeUpdate(BaseModel):
    player: Optional[str] = None
    version: Optional[str] = None
    quantity: Optional[int] = None
    buy: Optional[int] = None
    sell: Optional[int] = None
    platform: Optional[str] = None
    tag: Optional[str] = None
    notes: Optional[str] = None
    timestamp: Optional[str] = None  # ISO string

# (other models like UserSettings, TradingGoal, ExtSale etc remain unchanged...)

# --------- POOLS & APP ---------
pool = None
player_pool = None
watchlist_pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, player_pool, watchlist_pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    if PLAYER_DATABASE_URL == DATABASE_URL:
        player_pool = pool
    else:
        player_pool = await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)

    if WATCHLIST_DATABASE_URL == DATABASE_URL:
        watchlist_pool = pool
    else:
        watchlist_pool = await asyncpg.create_pool(WATCHLIST_DATABASE_URL, min_size=1, max_size=10)

    try:
        yield
    finally:
        to_close = {pool, player_pool, watchlist_pool}
        for p in to_close:
            if p is not None:
                await p.close()

app = FastAPI(lifespan=lifespan)

# --------- MIDDLEWARE (CORS FIRST) ---------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_origin_regex=r"^https://.*\.railway\.app$",
    allow_credentials=True,
    allow_methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"],
    allow_headers=["Authorization","Content-Type","X-Requested-With","Accept"],
    expose_headers=["Content-Disposition"],
    max_age=600,
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    same_site="none",
    https_only=True,
)

# --------- HELPERS ---------
async def get_db():
    async with pool.acquire() as connection:
        yield connection

def get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# --------- TRADES ROUTES ---------

@app.post("/api/trades")
async def add_trade(request: Request, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    data = await request.json()
    required_fields = ["player", "version", "buy", "sell", "quantity", "platform", "tag"]
    missing_fields = [f for f in required_fields if f not in data or data[f] == ""]
    if missing_fields:
        raise HTTPException(status_code=400, detail=f"Missing required fields: {missing_fields}")

    buy = int(data["buy"]); sell = int(data["sell"]); qty = int(data["quantity"])
    ea_tax = int(sell * qty * 0.05)
    profit = (sell - buy) * qty

    await conn.execute(
        """
        INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, notes, timestamp, trade_id)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),$12)
        """,
        user_id, data["player"], data["version"], buy, sell, qty,
        data["platform"], profit, ea_tax, data["tag"], data.get("notes",""), data.get("trade_id")
    )
    return {"message": "Trade added", "profit": profit, "ea_tax": ea_tax}

@app.get("/api/trades")
async def get_all_trades(user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    rows = await conn.fetch("SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC", user_id)
    return {"trades": [dict(r) for r in rows]}

@app.put("/api/trades/{trade_id}")
async def update_trade(trade_id: int, payload: TradeUpdate, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    data = payload.model_dump(exclude_none=True)
    if not data:
        raise HTTPException(status_code=400, detail="No fields to update")

    fields, values = [], []
    for col, val in data.items():
        fields.append(f"{col} = ${len(values)+1}")
        values.append(val)

    q = f"""
        UPDATE trades
           SET {', '.join(fields)}
         WHERE trade_id = ${len(values)+1}
           AND user_id  = ${len(values)+2}
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
    """
    values.extend([trade_id, user_id])
    row = await conn.fetchrow(q, *values)
    if not row:
        raise HTTPException(status_code=404, detail="Trade not found")

    # Recompute tax/profit
    sell = int(row["sell"] or 0)
    buy  = int(row["buy"] or 0)
    qty  = int(row["quantity"] or 1)
    ea_tax = int(round(sell * qty * 0.05))
    profit = (sell - buy) * qty

    row2 = await conn.fetchrow(
        """
        UPDATE trades
           SET ea_tax = $1, profit = $2
         WHERE trade_id = $3 AND user_id = $4
     RETURNING player, version, quantity, buy, sell, platform, tag, notes, ea_tax, profit, timestamp, trade_id
        """,
        ea_tax, profit, trade_id, user_id
    )
    return dict(row2)

@app.delete("/api/trades/{trade_id}")
async def delete_trade(trade_id: int, user_id: str = Depends(get_current_user), conn=Depends(get_db)):
    result = await conn.execute("DELETE FROM trades WHERE trade_id=$1 AND user_id=$2", trade_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Trade not found")
    return {"message": "Deleted"}
