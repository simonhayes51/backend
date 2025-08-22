from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import aiohttp
import asyncpg
import os

app = FastAPI()

# CORS: allow frontend origin
origins = [
    "https://frontend-production-ab5e.up.railway.app",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Load environment variables
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DATABASE_URL = os.getenv("DATABASE_URL")

@app.get("/login")
async def login():
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    )
    return RedirectResponse(discord_oauth_url)

@app.get("/callback")
async def callback(code: str):
    async with aiohttp.ClientSession() as session:
        data = {
            "client_id": DISCORD_CLIENT_ID,
            "client_secret": DISCORD_CLIENT_SECRET,
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "scope": "identify",
        }
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        async with session.post("https://discord.com/api/oauth2/token", data=data, headers=headers) as resp:
            token_response = await resp.json()
            access_token = token_response.get("access_token")

        if not access_token:
            raise HTTPException(status_code=400, detail="Failed to obtain access token")

        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            user = await resp.json()
            discord_id = user.get("id")

        if not discord_id:
            raise HTTPException(status_code=400, detail="Failed to fetch Discord user")

    conn = await asyncpg.connect(DATABASE_URL)
    existing = await conn.fetchrow("SELECT * FROM traders WHERE discord_id = $1", discord_id)

    if not existing:
        await conn.execute("INSERT INTO traders (discord_id) VALUES ($1)", discord_id)

    await conn.close()

    return RedirectResponse(f"https://frontend-production-ab5e.up.railway.app/?user_id={discord_id}")

@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT * FROM traders WHERE discord_id = $1", user_id)
    await conn.close()
    if row:
        return dict(row)
    return {"error": "User not found"}

# 👇👇👇 Add this to enable trade logging
class Trade(BaseModel):
    name: str
    version: str
    buyPrice: int
    sellPrice: int
    platform: str
    user_id: str

@app.post("/logtrade")
async def log_trade(trade: Trade):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        await conn.execute(
            """
            INSERT INTO trades (user_id, name, version, buy_price, sell_price, platform)
            VALUES ($1, $2, $3, $4, $5, $6)
            """,
            trade.user_id, trade.name, trade.version, trade.buyPrice, trade.sellPrice, trade.platform
        )
        await conn.close()
        return {"status": "success"}
    except Exception as e:
        print(f"[ERROR] Trade logging failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to log trade")