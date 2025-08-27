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
from typing import List, Optional

load_dotenv()

# --------- ENV ---------
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
PLAYER_DATABASE_URL = os.getenv("PLAYER_DATABASE_URL")
WATCHLIST_DATABASE_URL = os.getenv("WATCHLIST_DATABASE_URL", DATABASE_URL)  # 👈 NEW

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:5173")
PORT = int(os.getenv("PORT", 8000))

# --------- MODELS ---------
class WatchlistCreate(BaseModel):
    player_name: str
    card_id: int
    version: Optional[str] = None
    platform: str
    notes: Optional[str] = None

# --------- POOLS ---------
pool = None
player_pool = None
watchlist_pool = None  # 👈 NEW

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, player_pool, watchlist_pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    player_pool = await asyncpg.create_pool(PLAYER_DATABASE_URL, min_size=1, max_size=10)
    watchlist_pool = await asyncpg.create_pool(WATCHLIST_DATABASE_URL, min_size=1, max_size=10)

    try:
        yield
    finally:
        if pool: await pool.close()
        if player_pool: await player_pool.close()
        if watchlist_pool: await watchlist_pool.close()

app = FastAPI(lifespan=lifespan)

# --------- HELPERS ---------
async def get_db():
    async with pool.acquire() as conn:
        yield conn

async def get_watchlist_db():
    async with watchlist_pool.acquire() as conn:
        yield conn

def get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# --------- WATCHLIST ROUTES ---------
@app.post("/api/watchlist")
async def add_watch_item(payload: WatchlistCreate, user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    # fetch current price from fut.gg
    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://www.fut.gg/api/fut/player-prices/25/{payload.card_id}") as resp:
            data = await resp.json() if resp.status == 200 else {}

    current_price = data.get("data", {}).get("currentPrice", {}).get("price", None)
    extinct = data.get("data", {}).get("currentPrice", {}).get("isExtinct", False)

    await conn.execute(
        """INSERT INTO watchlist (user_id, card_id, player_name, version, platform,
                                  started_price, last_price, last_checked, notes)
           VALUES ($1,$2,$3,$4,$5,$6,$6,NOW(),$7)
           ON CONFLICT (user_id, card_id, platform) DO UPDATE
             SET last_price=$6, last_checked=NOW(), notes=$7""",
        user_id, payload.card_id, payload.player_name, payload.version, payload.platform,
        current_price or 0, payload.notes
    )
    return {"message": "Added to watchlist", "price": current_price, "is_extinct": extinct}

@app.get("/api/watchlist")
async def list_watch_items(user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    rows = await conn.fetch("SELECT * FROM watchlist WHERE user_id=$1 ORDER BY started_at DESC", user_id)
    return {"items": [dict(r) for r in rows]}

@app.delete("/api/watchlist/{watch_id}")
async def delete_watch_item(watch_id: int, user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    result = await conn.execute("DELETE FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id)
    if result == "DELETE 0":
        raise HTTPException(status_code=404, detail="Not found")
    return {"message": "Deleted"}

@app.post("/api/watchlist/{watch_id}/refresh")
async def refresh_watch_item(watch_id: int, user_id: str = Depends(get_current_user), conn = Depends(get_watchlist_db)):
    row = await conn.fetchrow("SELECT * FROM watchlist WHERE id=$1 AND user_id=$2", watch_id, user_id)
    if not row:
        raise HTTPException(status_code=404, detail="Not found")

    async with aiohttp.ClientSession() as session:
        async with session.get(f"https://www.fut.gg/api/fut/player-prices/25/{row['card_id']}") as resp:
            data = await resp.json() if resp.status == 200 else {}

    current_price = data.get("data", {}).get("currentPrice", {}).get("price", None)
    extinct = data.get("data", {}).get("currentPrice", {}).get("isExtinct", False)

    await conn.execute(
        "UPDATE watchlist SET last_price=$1, last_checked=NOW() WHERE id=$2",
        current_price, watch_id
    )
    return {"message": "Refreshed", "price": current_price, "is_extinct": extinct}

# --------- SEARCH (only new/changed part) ---------
@app.get("/api/search-players")
async def search_players(q: str = ""):
    """
    Autocomplete endpoint used by the Watchlist add form.
    Looks up players by name OR card_id in the PLAYER_DATABASE_URL's `fut_players` table.
    """
    q = (q or "").strip()
    if not q:
        return {"players": []}
    try:
        async with player_pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                  card_id,
                  name,
                  rating,
                  version,
                  image_url,
                  club,
                  nation,
                  position,
                  price
                FROM fut_players
                WHERE LOWER(name) LIKE LOWER($1)
                   OR card_id::text LIKE $1
                ORDER BY rating DESC NULLS LAST, name ASC
                LIMIT 20
                """,
                f"%{q}%"
            )
        players = [
            {
                "card_id": int(r["card_id"]),
                "name": r["name"],
                "rating": r["rating"],
                "version": r["version"],
                "image_url": r["image_url"],
                "club": r["club"],
                "nation": r["nation"],
                "position": r["position"],
                "price": r["price"],
            }
            for r in rows
        ]
        return {"players": players}
    except Exception as e:
        logging.error(f"Player search error: {e}")
        return {"players": [], "error": str(e)}

# --------- ENTRY ---------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
