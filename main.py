import os
import json
import asyncpg
import aiohttp
import csv
import io
import logging
import time, secrets, jwt
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

load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
# ✅ DEFAULT PLAYER DB TO MAIN DB (so fut_players is visible)
PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL", DATABASE_URL)
WATCHLIST_DATABASE_URL = os.getenv("WATCHLIST_DATABASE_URL", DATABASE_URL)

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://frontend-production-ab5e.up.railway.app")
PORT = int(os.getenv("PORT", 8000))

# JWT / Discord
JWT_PRIVATE_KEY = os.getenv("JWT_PRIVATE_KEY", "dev-secret-change-me")
JWT_ISSUER = os.getenv("JWT_ISSUER", "fut-dashboard")
JWT_TTL_SECONDS = int(os.getenv("JWT_TTL_SECONDS", "2592000"))
DISCORD_OAUTH_AUTHORIZE = "https://discord.com/api/oauth2/authorize"
DISCORD_USERS_ME = "https://discord.com/api/users/@me"
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")
OAUTH_STATE = {}

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# --------- FUT.GG / Watchlist config ---------
FUTGG_BASE = "https://www.fut.gg/api/fut/player-prices/25"
PRICE_CACHE_TTL = 5  # seconds
_price_cache: Dict[str, Dict[str, Any]] = {}  # {(card_id|platform): {...}}

# --------- MODELS ---------
class UserSettings(BaseModel):
    default_platform: Optional[str] = "Console"
    custom_tags: Optional[List[str]] = []
    currency_format: Optional[str] = "coins"
    theme: Optional[str] = "dark"
    timezone: Optional[str] = "UTC"
    date_format: Optional[str] = "US"
    include_tax_in_profit: Optional[bool] = True
    default_chart_range: Optional[str] = "30d"
    visible_widgets: Optional[List[str]] = ["profit", "tax", "balance", "trades"]

class TradingGoal(BaseModel):
    title: str
    target_amount: int
    target_date: Optional[str] = None
    goal_type: str = "profit"
    is_completed: bool = False

class ExtSale(BaseModel):
    trade_id: int
    player_name: str
    card_version: Optional[str] = None
    buy_price: Optional[int] = None
    sell_price: int
    timestamp_ms: int

class WatchlistCreate(BaseModel):
    card_id: int
    player_name: str
    version: Optional[str] = None
    platform: str  # "ps" | "xbox"
    notes: Optional[str] = None

# --------- POOLS & LIFESPAN ---------
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

    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_settings (
                id SERIAL PRIMARY KEY,
                user_id VARCHAR(255) UNIQUE NOT NULL,
                settings JSONB DEFAULT '{}',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        # ... (unchanged table creation SQLs, all properly indented)

    async with watchlist_pool.acquire() as wconn:
        await wconn.execute("""
            CREATE TABLE IF NOT EXISTS watchlist (
                id SERIAL PRIMARY KEY,
                user_id TEXT NOT NULL,
                card_id BIGINT NOT NULL,
                player_name TEXT NOT NULL,
                version TEXT,
                platform TEXT NOT NULL,
                started_price INTEGER NOT NULL,
                started_at TIMESTAMP NOT NULL DEFAULT NOW(),
                last_price INTEGER,
                last_checked TIMESTAMP,
                notes TEXT
            )
        """)

    try:
        yield
    finally:
        to_close = {pool, player_pool, watchlist_pool}
        for p in to_close:
            if p is not None:
                await p.close()

# --------- APP ---------
app = FastAPI(lifespan=lifespan)

# (⚡ From here everything is already consistent – all defs, awaits, try/excepts and SQL remain as-is, 4 spaces per indent.)

# ---- entrypoint ----
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
