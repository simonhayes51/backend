from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
import asyncpg
import aiohttp
import os

app = FastAPI()

DATABASE_URL = os.getenv("DATABASE_URL")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = "https://backend-production-1f1a.up.railway.app/callback"

origins = ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        portfolio = await conn.fetchrow("SELECT starting_balance FROM portfolio WHERE user_id=$1", user_id)
        stats = await conn.fetchrow("SELECT SUM(profit) AS total_profit, SUM(ea_tax) AS total_tax, COUNT(*) AS count FROM trades WHERE user_id=$1", user_id)
        await conn.close()
        return {
            "starting_balance": portfolio["starting_balance"] if portfolio else 0,
            "total_profit": stats["total_profit"] or 0,
            "total_tax": stats["total_tax"] or 0,
            "trades": stats["count"]
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/sales/{user_id}")
async def get_sales(user_id: str):
    try:
        conn = await asyncpg.connect(DATABASE_URL)
        rows = await conn.fetch("SELECT player, quantity, sell, profit, timestamp FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10", user_id)
        await conn.close()
        return [dict(row) for row in rows]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/login")
async def login():
    discord_auth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    )
    return RedirectResponse(discord_auth_url)

@app.get("/callback")
async def callback(code: str):
    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    async with aiohttp.ClientSession() as session:
        async with session.post(token_url, data=data, headers=headers) as resp:
            token_response = await resp.json()
            access_token = token_response.get("access_token")

    user_url = "https://discord.com/api/users/@me"
    headers = {"Authorization": f"Bearer {access_token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(user_url, headers=headers) as resp:
            user_info = await resp.json()

    discord_id = user_info["id"]
    frontend_url = f"https://frontend-production-aa68.up.railway.app/?user_id={discord_id}"
    return RedirectResponse(frontend_url)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8080)