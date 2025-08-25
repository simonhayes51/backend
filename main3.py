import os
import json
import asyncpg
import aiohttp
import csv
import io
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Query
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Optional
import logging
import requests
from bs4 import BeautifulSoup

load_dotenv()

# Environment variable validation
required_env_vars = ["DATABASE_URL", "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "DISCORD_REDIRECT_URI"]
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    raise ValueError(f"Missing required environment variables: {missing_vars}")

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY")
FRONTEND_URL = os.getenv("FRONTEND_URL", "https://frontend-production-ab5e.up.railway.app")

# Discord variables
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
DISCORD_SERVER_ID = os.getenv("DISCORD_SERVER_ID")

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# Pydantic models
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

# Global pool + players DB
pool = None
PLAYERS_DB = []

# Discord helpers
async def get_discord_user_info(access_token: str):
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        ) as resp:
            return await resp.json() if resp.status == 200 else None

async def check_server_membership(user_id: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"https://discord.com/api/guilds/{DISCORD_SERVER_ID}/members/{user_id}",
                headers={"Authorization": f"Bot {DISCORD_BOT_TOKEN}"}
            ) as resp:
                return resp.status == 200
    except Exception:
        return False

# App lifespan for DB + players
@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool, PLAYERS_DB
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)

    # Load players DB
    try:
        with open("players_temp.json", "r", encoding="utf-8") as f:
            PLAYERS_DB = json.load(f)
            print(f"✅ Loaded {len(PLAYERS_DB)} players.")
    except Exception as e:
        print(f"❌ Failed to load players: {e}")
        PLAYERS_DB = []

    yield
    if pool:
        await pool.close()

app = FastAPI(lifespan=lifespan)

# Port for Railway
PORT = int(os.getenv("PORT", 8000))

# Middleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://*.railway.app",
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# DB dependency
async def get_db():
    async with pool.acquire() as connection:
        yield connection

def get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# OAuth login
@app.get("/api/login")
async def login():
    return RedirectResponse(
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

# ================================
# NEW PRICECHECK ENDPOINT (UPDATED)
# ================================
@app.get("/api/pricecheck")
async def price_check(
    player_name: str = Query(...),
    platform: str = Query("console"),
    user_id: str = Depends(get_current_user)
):
    try:
        # Match player from PLAYERS_DB by name + rating
        matched_player = next(
            (p for p in PLAYERS_DB if f"{p['name'].lower()} {p['rating']}" == player_name.lower()), None
        )
        if not matched_player:
            raise HTTPException(status_code=404, detail="Player not found")

        # FUTBIN URL
        player_id = matched_player["id"]
        player_name_clean = matched_player["name"]
        slug = player_name_clean.replace(" ", "-").lower()
        futbin_url = f"https://www.futbin.com/25/player/{player_id}/{slug}"

        headers = {"User-Agent": "Mozilla/5.0"}
        response = requests.get(futbin_url, headers=headers)
        if response.status_code != 200:
            raise HTTPException(status_code=500, detail="Failed to fetch FUTBIN data")

        # Parse HTML for price data
        soup = BeautifulSoup(response.text, "html.parser")
        prices_wrapper = soup.find("div", class_="lowest-prices-wrapper")
        if not prices_wrapper:
            raise HTTPException(status_code=500, detail="Prices not found on FUTBIN")

        price_elements = prices_wrapper.find_all("div", class_="lowest-price")

        def get_price_text(index):
            if len(price_elements) > index:
                return price_elements[index].text.strip().replace(",", "").replace("\n", "")
            return "0"

        # Console = PS + Xbox
        if platform.lower() == "console":
            ps_price = get_price_text(0)
            xbox_price = get_price_text(1)
            price = ps_price if ps_price != "0" else xbox_price
        elif platform.lower() == "pc":
            price = get_price_text(2)
        else:
            price = "0"

        # Format price
        price = "N/A" if price == "0" or price == "" else f"{int(price):,}"

        # Response matches Discord bot format
        return {
            "player": player_name_clean,
            "rating": matched_player["rating"],
            "platform": platform.capitalize(),
            "price": price,
            "source": "FUTBIN"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Price check failed: {str(e)}")

# ================================
# REST OF YOUR EXISTING ENDPOINTS
# ================================

# Keep all your other routes exactly as before...
# OAuth callback, portfolio, trades, settings, dashboard, analytics, etc.

@app.get("/")
async def root():
    return {"message": "FUT Dashboard API", "status": "healthy"}

# For Railway deployment
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
