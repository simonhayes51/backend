# Add these endpoints to your existing main.py

import csv
import io
import json
from fastapi import UploadFile, File
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from typing import List, Optional

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

class BulkTradeImport(BaseModel):
    player: str
    version: str
    buy: int
    sell: int
    quantity: int
    platform: str
    tag: Optional[str] = ""

# User settings table (add this to your database schema)
"""
CREATE TABLE IF NOT EXISTS user_settings (
    id SERIAL PRIMARY KEY,
    user_id VARCHAR(255) UNIQUE NOT NULL,
    settings JSONB DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

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

# Data export endpoints
@app.get("/api/export/trades")
async def export_trades(
    format: str = "csv",  # csv or json
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
            
            def generate():
                yield json_data
                
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
            
            response = StreamingResponse(
                io.StringIO(output.getvalue()),
                media_type="text/csv",
                headers={"Content-Disposition": "attachment; filename=trades_export.csv"}
            )
            return response
            
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
            "DELETE FROM trades WHERE user_id=$1 RETURNING count(*)",
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

# Custom tags management
@app.get("/api/tags")
async def get_user_tags(
    user_id: str = Depends(get_current_user),
    conn = Depends(get_db)
):
    try:
        # Get custom tags from settings
        settings = await conn.fetchrow(
            "SELECT settings FROM user_settings WHERE user_id=$1",
            user_id
        )
        
        custom_tags = []
        if settings and settings["settings"]:
            custom_tags = settings["settings"].get("custom_tags", [])
        
        # Get tags from existing trades
        used_tags = await conn.fetch(
            "SELECT DISTINCT tag FROM trades WHERE user_id=$1 AND tag IS NOT NULL AND tag != ''",
            user_id
        )
        
        used_tag_names = [row["tag"] for row in used_tags]
        
        # Combine and deduplicate
        all_tags = list(set(custom_tags + used_tag_names))
        
        return {
            "custom_tags": custom_tags,
            "used_tags": used_tag_names,
            "all_tags": sorted(all_tags)
        }
        
    except Exception as e:
        raise HTTPException(status_code=500, detail="Failed to fetch tags")
