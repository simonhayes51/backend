from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
import aiohttp
import asyncpg
import os
import json

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

# Session middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "supersecret"),
    same_site="none",
    https_only=True
)

# ENV variables
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")
DATABASE_URL = os.getenv("DATABASE_URL")

# ---------- ROUTES ----------

@app.get("/login")
async def login():
    discord_oauth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={REDIRECT_URI}&response_type=code&scope=identify"
    )
    return RedirectResponse(discord_oauth_url)


@app.get("/callback")
async def callback(request: Request, code: str):
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
            text = await resp.text()
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to obtain access token")
            token_response = json.loads(text)
            access_token = token_response.get("access_token")

        if not access_token:
            raise HTTPException(status_code=400, detail="No access token received from Discord")

        # Get user info
        headers = {"Authorization": f"Bearer {access_token}"}
        async with session.get("https://discord.com/api/users/@me", headers=headers) as resp:
            if resp.status != 200:
                raise HTTPException(status_code=400, detail="Failed to get user info")
            user = await resp.json()
            discord_id = user["id"]

    # Store user in DB
    conn = await asyncpg.connect(DATABASE_URL)
    existing = await conn.fetchrow("SELECT * FROM traders WHERE discord_id = $1", discord_id)
    if not existing:
        await conn.execute("INSERT INTO traders (user_id, discord_id) VALUES ($1, $1)", discord_id)
    await conn.close()

    # Store session
    request.session["user_id"] = discord_id
    print("[DEBUG] Set session user_id:", discord_id)

    return RedirectResponse(f"https://frontend-production-ab5e.up.railway.app/?user_id={discord_id}")


@app.get("/api/profile/{user_id}")
async def get_profile(user_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("SELECT * FROM traders WHERE discord_id = $1", user_id)
    await conn.close()
    if row:
        return dict(row)
    else:
        return {"error": "User not found"}


@app.post("/logtrade")
async def log_trade(request: Request):
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not logged in")

    data = await request.json()
    player = data.get("name")
    version = data.get("version")
    buy = int(data.get("buyPrice"))
    sell = int(data.get("sellPrice"))
    platform = data.get("platform")
    quantity = int(data.get("quantity", 1))
    tag = data.get("tag", "")
    notes = data.get("notes", "")

    if not all([player, version, buy, sell, platform]):
        raise HTTPException(status_code=400, detail="Missing fields")

    ea_tax = int(sell * 0.05) * quantity
    profit = (sell - buy) * quantity - ea_tax

    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, tag, notes, ea_tax, profit)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
    """, user_id, player, version, buy, sell, quantity, platform, tag, notes, ea_tax, profit)
    await conn.close()

    return {"status": "Trade logged!"}


@app.get("/api/trades/{user_id}")
async def get_trades(user_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    rows = await conn.fetch("""
        SELECT player, version, buy, sell, quantity, platform, tag, notes, ea_tax, profit, timestamp
        FROM trades
        WHERE user_id = $1
        ORDER BY timestamp DESC
    """, user_id)
    await conn.close()
    return [dict(row) for row in rows]


@app.get("/api/stats/{user_id}")
async def get_stats(user_id: str):
    conn = await asyncpg.connect(DATABASE_URL)
    row = await conn.fetchrow("""
        SELECT 
            COALESCE(SUM(profit), 0) AS net_profit,
            COALESCE(SUM(ea_tax), 0) AS total_tax
        FROM trades
        WHERE user_id = $1
    """, user_id)
    await conn.close()
    return dict(row) if row else {"net_profit": 0, "total_tax": 0}
