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

# Session middleware with proper cross-origin support
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "supersecret"),
    same_site="none",
    https_only=True
)

# ENV vars
DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI")  # must match Discord settings
DATABASE_URL = os.getenv("DATABASE_URL")


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
            print("[DEBUG] Discord token response:", text)
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
        await conn.execute("INSERT INTO traders (discord_id) VALUES ($1)", discord_id)
    await conn.close()

    # Store session
    request.session["user_id"] = discord_id
    print("[DEBUG] Set session user_id:", discord_id)
    
    # Redirect to frontend
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
    user_id = request.headers.get("x-user-id")
    if not user_id:
        raise HTTPException(status_code=401, detail="User not logged in")

    data = await request.json()
    name = data.get("name")
    version = data.get("version")
    buy_price = data.get("buyPrice")
    sell_price = data.get("sellPrice")
    platform = data.get("platform")

    if not all([name, version, buy_price, sell_price, platform]):
        raise HTTPException(status_code=400, detail="Missing trade fields")

    conn = await asyncpg.connect(DATABASE_URL)
    await conn.execute("""
        INSERT INTO trades (discord_id, name, version, buy_price, sell_price, platform)
        VALUES ($1, $2, $3, $4, $5, $6)
    """, user_id, name, version, int(buy_price), int(sell_price), platform)
    await conn.close()

    return {"status": "success"}