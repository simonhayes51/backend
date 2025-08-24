import os
import json
import asyncpg
import aiohttp
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from bs4 import BeautifulSoup
import requests
import re

# Load environment variables
load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")

# Initialize FastAPI app
app = FastAPI()

# Middleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://frontend-production-ab5e.up.railway.app",
        "http://localhost:5173",
        "http://localhost:3000",
        "*"
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- DATABASE CONNECTION ---------------- #
async def get_db():
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        yield conn
    finally:
        await conn.close()

# ---------------- DISCORD LOGIN ---------------- #
@app.get("/login")
async def login():
    return RedirectResponse(
        url=f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
            f"&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing OAuth code")

    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=data, headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch token")
            token_json = await resp.json()

        # Fetch user info from Discord
        user_url = "https://discord.com/api/users/@me"
        headers = {"Authorization": f"Bearer {token_json['access_token']}"}
        async with session.get(user_url, headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to fetch user info")
            user_data = await resp.json()

    # Save session
    request.session["user"] = user_data
    return RedirectResponse(url="/")

@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    return {"status": "ok", "user_id": user_id}

# ---------------- PRICE CHECK ENDPOINT ---------------- #
@app.get("/api/pricecheck")
async def price_check(player: str):
    """
    Fetch the latest price for a FUT player from FUT.GG.
    Example: /api/pricecheck?player=Erling Haaland
    """
    try:
        # Format player name into URL-friendly format
        search_name = player.lower().replace(" ", "-")
        url = f"https://www.fut.gg/players/?name={search_name}"

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/115.0.0.0 Safari/537.36"
        }

        # Search FUT.GG
        resp = requests.get(url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Failed to fetch data from FUT.GG")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Find the first player result block
        player_link = soup.find("a", href=re.compile(r"^/players/\d+-"))
        if not player_link:
            raise HTTPException(status_code=404, detail="Player not found")

        # Build the player's specific page URL
        player_url = f"https://www.fut.gg{player_link['href']}"

        # Fetch player page
        resp = requests.get(player_url, headers=headers)
        if resp.status_code != 200:
            raise HTTPException(status_code=404, detail="Failed to fetch player details")

        soup = BeautifulSoup(resp.text, "html.parser")

        # Locate price container
        price_block = soup.find(
            "div",
            class_="font-bold text-2xl flex flex-row items-center gap-1 justify-self-end"
        )

        if not price_block:
            return {"player": player, "price": None, "note": "No BIN price available (SBC/Objective)"}

        # Extract price (remove commas and convert to int)
        raw_price = price_block.get_text(strip=True).replace(",", "")
        price = int(re.sub(r"\D", "", raw_price))

        return {
            "player": player,
            "price": price,
            "platform": "console"
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Price check failed: {str(e)}")
