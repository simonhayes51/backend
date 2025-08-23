import os
import json
import asyncpg
import aiohttp
from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
SECRET_KEY = os.getenv("SECRET_KEY", "supersecretkey")

app = FastAPI()

# Middleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with frontend domain later for security
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Database connection ---
async def get_db():
    return await asyncpg.connect(DATABASE_URL)

# --- Discord OAuth Login ---
@app.get("/login")
async def login():
    return RedirectResponse(
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.get("/callback")
async def callback(request: Request):
    code = request.query_params.get("code")
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

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
            token_data = await resp.json()
            if "access_token" not in token_data:
                raise HTTPException(status_code=400, detail="OAuth failed")
            access_token = token_data["access_token"]

        async with session.get("https://discord.com/api/users/@me", headers={"Authorization": f"Bearer {access_token}"}) as resp:
            user_data = await resp.json()
            user_id = user_data["id"]
            request.session["user_id"] = user_id
            return RedirectResponse(f"https://frontend-production-aa68.up.railway.app/?user_id={user_id}")

# --- Fetch summary stats ---
@app.get("/summary")
async def get_summary(user_id: str = Query(...)):
    try:
        conn = await get_db()

        # Get portfolio balance
        portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)
        # Get trading stats
        stats = await conn.fetchrow(
            "SELECT COALESCE(SUM(profit),0) AS total_profit, COALESCE(SUM(ea_tax),0) AS total_tax FROM trades WHERE user_id=$1",
            user_id,
        )
        await conn.close()

        return {
            "netProfit": stats["total_profit"] or 0,
            "taxPaid": stats["total_tax"] or 0,
            "startingBalance": portfolio["starting_balance"] if portfolio else 0
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Fetch trades for a user ---
@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    try:
        conn = await get_db()
        rows = await conn.fetch(
            "SELECT player, version, buy, sell, quantity, platform, profit, ea_tax, timestamp FROM trades WHERE user_id=$1 ORDER BY timestamp DESC",
            user_id,
        )
        await conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Fetch sales history ---
@app.get("/api/sales/{user_id}")
async def get_sales(user_id: str):
    try:
        conn = await get_db()
        rows = await conn.fetch(
            "SELECT player, quantity, sell, profit, timestamp FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10",
            user_id,
        )
        await conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))