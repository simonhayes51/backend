import os
import json
import asyncpg
import aiohttp
import csv
import io
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from dotenv import load_dotenv
from contextlib import asynccontextmanager
from pydantic import BaseModel
from typing import List, Optional
import logging

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

if not SECRET_KEY:
    raise ValueError("SECRET_KEY environment variable is required")

# Pydantic models for settings
class UserSettings(BaseModel):
    default_platform: Optional[str] = "Console"
    custom_tags: Optional[List[str]] = []
    currency_format: Optional[str] = "coins"  # coins, k, m
    theme: Optional[str] = "dark"
    timezone: Optional[str] = "UTC"
    date_format: Optional[str] = "US"  # US or EU
    include_tax_in_profit: Optional[bool] = True
    default_chart_range: Optional[str] = "30d"  # 7d, 30d, 90d, all
    visible_widgets: Optional[List[str]] = ["profit", "tax", "balance", "trades"]

# Global connection pool
pool = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=10)
    
    # Create user_settings table
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
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_user_settings_user_id ON user_settings(user_id)
        """)
    
    yield
    # Shutdown
    if pool:
        await pool.close()

app = FastAPI(lifespan=lifespan)

# Get port from Railway environment
PORT = int(os.getenv("PORT", 8000))

# Middleware
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "https://*.railway.app",  # Allow all Railway domains
        "http://localhost:5173",
        "http://localhost:3000",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Dependencies
async def get_db():
    async with pool.acquire() as connection:
        yield connection

def get_current_user(request: Request) -> str:
    user_id = request.session.get("user_id")
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user_id

# OAuth Routes
@app.get("/api/login")
async def login():
    return RedirectResponse(
        f"https://discord.com/oauth2/authorize?client_id={DISCORD_CLIENT_ID}&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify"
    )

@app.get("/api/callback")
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

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(token_url, data=data, headers=headers) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=400, detail="OAuth token exchange failed")
                token_data = await resp.json()
                
                if "access_token" not in token_data:
                    raise HTTPException(status_code=400, detail="OAuth failed")
                access_token = token_data["access_token"]

            async with session.get(
                "https://discord.com/api/users/@me",
                headers={"Authorization": f"Bearer {access_token}"}
            ) as resp:
                if resp.status != 200:
                    raise HTTPException(status_code=400, detail="Failed to fetch user data")
                user_data = await resp.json()
                user_id = user_data["id"]
                
                # Store in session
                request.session["user_id"] = user_id
                
                # Initialize user's portfolio if it doesn't exist
                async with pool.acquire() as conn:
                    await conn.execute(
                        "INSERT INTO portfolio (user_id, starting_balance) VALUES ($1, $2) ON CONFLICT (user_id) DO NOTHING",
                        user_id, 0
                    )
                
                return RedirectResponse(f"{FRONTEND_URL}/?authenticated=true")
    except Exception as e:
        logging.error(f"OAuth error: {e}")
        raise HTTPException(status_code=500, detail="Authentication failed")

@app.get("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"message": "Logged out successfully"}

@app.get("/api/me")
async def get_current_user_info(user_id: str = Depends(get_current_user)):
    return {"user_id": user_id, "authenticated": True}

# Settings endpoints
@app.get("/api/settings")
async def get_user_settings(
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        settings = await conn.fetchrow(
            "SELECT settings FROM user_settings WHERE user_id=$1",
            user_id
        )
        
        if settings:
            return settings["settings"]
        else:
            # Return default settings
            default_settings = UserSettings()
            return default_settings.dict()
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch settings")

@app.post("/api/settings")
async def update_user_settings(
    settings: UserSettings,
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        await conn.execute(
            """
            INSERT INTO user_settings (user_id, settings, updated_at) 
            VALUES ($1, $2, NOW())
            ON CONFLICT (user_id) 
            DO UPDATE SET settings = $2, updated_at = NOW()
            """,
            user_id, json.dumps(settings.dict())
        )
        return {"message": "Settings updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to update settings")

@app.post("/api/portfolio/balance")
async def update_starting_balance(
    request: Request,
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        data = await request.json()
        starting_balance = int(data.get("starting_balance", 0))
        
        await conn.execute(
            "INSERT INTO portfolio (user_id, starting_balance) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO UPDATE SET starting_balance = $2",
            user_id, starting_balance
        )
        
        return {"message": "Starting balance updated successfully"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to update starting balance")

# Data export endpoints
@app.get("/api/export/trades")
async def export_trades(
    format: str = "csv",
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        trades = await conn.fetch(
            "SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC",
            user_id
        )
        
        trades_data = [dict(trade) for trade in trades]
        
        if format.lower() == "json":
            # JSON export
            json_data = json.dumps(trades_data, indent=2, default=str)
            
            return StreamingResponse(
                io.BytesIO(json_data.encode()),
                media_type="application/json",
                headers={"Content-Disposition": "attachment; filename=trades_export.json"}
            )
        
        else:
            # CSV export
            output = io.StringIO()
            if trades_data:
                writer = csv.DictWriter(output, fieldnames=trades_data[0].keys())
                writer.writeheader()
                writer.writerows(trades_data)
            
            return StreamingResponse(
                io.StringIO(output.getvalue()),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=trades_export.csv"}
            )
            
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to export data")

# Data import endpoint
@app.post("/api/import/trades")
async def import_trades(
    file: UploadFile = File(...),
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        contents = await file.read()
        
        if file.filename.endswith('.json'):
            # JSON import
            data = json.loads(contents.decode('utf-8'))
            trades_to_import = data if isinstance(data, list) else [data]
        
        elif file.filename.endswith('.csv'):
            # CSV import
            csv_data = contents.decode('utf-8')
            reader = csv.DictReader(io.StringIO(csv_data))
            trades_to_import = list(reader)
        
        else:
            raise HTTPException(status_code=400, detail="Unsupported file format")
        
        imported_count = 0
        errors = []
        
        for trade_data in trades_to_import:
            try:
                # Validate and clean data
                player = trade_data.get('player', '').strip()
                version = trade_data.get('version', '').strip()
                buy = int(trade_data.get('buy', 0))
                sell = int(trade_data.get('sell', 0))
                quantity = int(trade_data.get('quantity', 1))
                platform = trade_data.get('platform', 'Console').strip()
                tag = trade_data.get('tag', '').strip()
                
                if not player or not version or buy <= 0 or sell <= 0:
                    errors.append(f"Invalid trade data: {trade_data}")
                    continue
                
                # Calculate profit and tax
                profit = (sell - buy) * quantity
                ea_tax = int(sell * quantity * 0.05)
                
                # Insert trade
                await conn.execute(
                    """
                    INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
                    """,
                    user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag
                )
                
                imported_count += 1
                
            except Exception as e:
                errors.append(f"Error importing trade {trade_data}: {str(e)}")
        
        return {
            "message": f"Successfully imported {imported_count} trades",
            "imported_count": imported_count,
            "errors": errors[:10]  # Limit error messages
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Import failed: {str(e)}")

# Delete all data endpoint
@app.delete("/api/data/delete-all")
async def delete_all_user_data(
    confirm: bool = False,
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    
    try:
        # Delete all trades
        trades_deleted = await conn.fetchval(
            "DELETE FROM trades WHERE user_id=$1",
            user_id
        )
        
        # Reset portfolio
        await conn.execute(
            "UPDATE portfolio SET starting_balance = 0 WHERE user_id=$1",
            user_id
        )
        
        return {
            "message": "All data deleted successfully",
            "trades_deleted": trades_deleted or 0
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to delete data")

# Dashboard Logic
async def fetch_dashboard_data(user_id: str, conn):
    try:
        # Fetch portfolio
        portfolio = await conn.fetchrow(
            "SELECT starting_balance FROM portfolio WHERE user_id=$1", 
            user_id
        )
        
        # Fetch aggregate stats
        stats = await conn.fetchrow(
            "SELECT COALESCE(SUM(profit),0) AS total_profit, COALESCE(SUM(ea_tax),0) AS total_tax, COUNT(*) as total_trades FROM trades WHERE user_id=$1",
            user_id,
        )

        # Fetch recent trades
        trades = await conn.fetch(
            "SELECT player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp "
            "FROM trades WHERE user_id=$1 ORDER BY timestamp DESC LIMIT 10",
            user_id,
        )

        # Calculate metrics
        all_trades = await conn.fetch(
            "SELECT profit FROM trades WHERE user_id=$1 ORDER BY timestamp DESC",
            user_id,
        )
        
        win_count = len([t for t in all_trades if t["profit"] and t["profit"] > 0])
        win_rate = round((win_count / len(all_trades)) * 100, 1) if all_trades else 0

        # Most used tag
        tag_stats = await conn.fetch(
            "SELECT tag, COUNT(*) as count FROM trades WHERE user_id=$1 GROUP BY tag ORDER BY count DESC LIMIT 1",
            user_id
        )
        most_used_tag = tag_stats[0]["tag"] if tag_stats else "N/A"

        # Best trade
        best_trade = await conn.fetchrow(
            "SELECT * FROM trades WHERE user_id=$1 ORDER BY profit DESC LIMIT 1",
            user_id
        )

        return {
            "netProfit": stats["total_profit"] or 0,
            "taxPaid": stats["total_tax"] or 0,
            "startingBalance": portfolio["starting_balance"] if portfolio else 0,
            "trades": [dict(row) for row in trades],
            "profile": {
                "totalProfit": stats["total_profit"] or 0,
                "tradesLogged": stats["total_trades"] or 0,
                "winRate": win_rate,
                "mostUsedTag": most_used_tag,
                "bestTrade": dict(best_trade) if best_trade else None,
            }
        }
    except Exception as e:
        logging.error(f"Dashboard fetch error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch dashboard data")

# Protected API Routes
@app.get("/api/dashboard")
async def get_dashboard(
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    return await fetch_dashboard_data(user_id, conn)

@app.get("/api/profile")
async def get_profile(
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    return await fetch_dashboard_data(user_id, conn)

@app.post("/api/trades")
async def add_trade(
    request: Request,
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        data = await request.json()
        
        # Validate required fields
        required_fields = ["player", "version", "buy", "sell", "quantity", "platform", "tag"]
        missing_fields = [field for field in required_fields if field not in data or data[field] == ""]
        if missing_fields:
            raise HTTPException(status_code=400, detail=f"Missing required fields: {missing_fields}")

        # Validate and convert numeric fields
        try:
            quantity = int(data["quantity"])
            buy = int(data["buy"])
            sell = int(data["sell"])
            if quantity <= 0 or buy <= 0 or sell <= 0:
                raise ValueError("Numeric values must be positive")
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="Invalid numeric values")

        # Calculate profit and tax
        profit = (sell - buy) * quantity
        ea_tax = int(sell * quantity * 0.05)

        # Insert trade
        await conn.execute(
            """
            INSERT INTO trades (user_id, player, version, buy, sell, quantity, platform, profit, ea_tax, tag, timestamp)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,NOW())
            """,
            user_id,
            data["player"],
            data["version"],
            buy,
            sell,
            quantity,
            data["platform"],
            profit,
            ea_tax,
            data["tag"]
        )
        
        return {"message": "Trade added successfully!", "profit": profit, "ea_tax": ea_tax}
        
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Add trade error: {e}")
        raise HTTPException(status_code=500, detail="Failed to add trade")

@app.get("/api/trades")
async def get_all_trades(
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        trades = await conn.fetch(
            "SELECT * FROM trades WHERE user_id=$1 ORDER BY timestamp DESC",
            user_id
        )
        return {"trades": [dict(row) for row in trades]}
    except Exception as e:
        logging.error(f"Get trades error: {e}")
        raise HTTPException(status_code=500, detail="Failed to fetch trades")

@app.delete("/api/trades/{trade_id}")
async def delete_trade(
    trade_id: int,
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        result = await conn.execute(
            "DELETE FROM trades WHERE id=$1 AND user_id=$2",
            trade_id, user_id
        )
        if result == "DELETE 0":
            raise HTTPException(status_code=404, detail="Trade not found")
        return {"message": "Trade deleted successfully"}
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Delete trade error: {e}")
        raise HTTPException(status_code=500, detail="Failed to delete trade")

@app.get("/health")
async def health_check():
    try:
        # Test database connection
        if pool:
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            return {"status": "healthy", "database": "connected"}
        else:
            return {"status": "unhealthy", "database": "disconnected"}
    except Exception as e:
        return {"status": "unhealthy", "database": "disconnected", "error": str(e)}

@app.get("/")
async def root():
    return {"message": "FUT Dashboard API", "status": "healthy"}

# For Railway deployment
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
