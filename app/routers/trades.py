# app/routers/trades.py
from fastapi import APIRouter, Depends, Request, HTTPException
from typing import Dict, Any, List
from pydantic import BaseModel, Field, validator
from app.db import get_db
from app.auth.entitlements import compute_entitlements
from datetime import datetime

router = APIRouter(prefix="/api/trades", tags=["Trades"])

class TradeItem(BaseModel):
    player: str = Field(..., min_length=1)
    version: str = Field(default="Standard")
    buy: int = Field(..., gt=0)
    sell: int = Field(..., gt=0)
    quantity: int = Field(default=1, gt=0, le=100)
    platform: str = Field(default="ps")
    tag: str = Field(default="")
    notes: str = Field(default="")

    @validator('sell')
    def sell_must_be_positive(cls, v, values):
        if v <= 0:
            raise ValueError('Sell price must be positive')
        return v

class BulkTradesRequest(BaseModel):
    trades: List[TradeItem] = Field(..., min_items=1, max_items=100)

@router.post("/bulk")
async def bulk_insert_trades(
    req: Request,
    body: BulkTradesRequest,
    db=Depends(get_db),
) -> Dict[str, Any]:
    """
    Insert up to 100 trades at once.
    Validate all before insertion.
    """
    try:
        ent = await compute_entitlements(req)
        user_id = ent.get("user_id")

        if not user_id:
            raise HTTPException(status_code=401, detail="Not authenticated")

        if len(body.trades) > 100:
            raise HTTPException(
                status_code=400,
                detail="Maximum 100 trades allowed per bulk insert"
            )

        # Validate all trades first
        validated_trades = []
        errors = []

        for idx, trade in enumerate(body.trades):
            # Calculate profit and EA tax
            ea_tax_rate = 0.05  # 5% EA tax
            sell_after_tax = int(trade.sell * (1 - ea_tax_rate))
            ea_tax = trade.sell - sell_after_tax
            profit = (sell_after_tax - trade.buy) * trade.quantity

            # Validation checks
            if trade.buy >= trade.sell:
                errors.append({
                    "index": idx,
                    "error": f"Buy price ({trade.buy}) must be less than sell price ({trade.sell})"
                })
                continue

            if not trade.player or len(trade.player.strip()) == 0:
                errors.append({
                    "index": idx,
                    "error": "Player name cannot be empty"
                })
                continue

            validated_trades.append({
                "user_id": user_id,
                "player": trade.player.strip(),
                "version": trade.version,
                "buy": trade.buy,
                "sell": trade.sell,
                "quantity": trade.quantity,
                "platform": trade.platform,
                "profit": profit,
                "ea_tax": ea_tax * trade.quantity,
                "tag": trade.tag or None,
                "notes": trade.notes or None,
                "timestamp": datetime.utcnow()
            })

        # If any validation errors, return them
        if errors:
            return {
                "ok": False,
                "message": f"Validation failed for {len(errors)} trade(s)",
                "errors": errors,
                "valid_count": len(validated_trades),
                "total_count": len(body.trades)
            }

        # Insert all validated trades
        insert_query = """
            INSERT INTO trades (
                user_id, player, version, buy, sell, quantity,
                platform, profit, ea_tax, tag, notes, timestamp
            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)
        """

        inserted_count = 0
        for trade_data in validated_trades:
            try:
                await db.execute(
                    insert_query,
                    trade_data["user_id"],
                    trade_data["player"],
                    trade_data["version"],
                    trade_data["buy"],
                    trade_data["sell"],
                    trade_data["quantity"],
                    trade_data["platform"],
                    trade_data["profit"],
                    trade_data["ea_tax"],
                    trade_data["tag"],
                    trade_data["notes"],
                    trade_data["timestamp"]
                )
                inserted_count += 1
            except Exception as e:
                import logging
                logging.error(f"Failed to insert trade: {e}")
                # Continue with other trades

        return {
            "ok": True,
            "message": f"Successfully inserted {inserted_count} trade(s)",
            "inserted_count": inserted_count,
            "total_count": len(body.trades),
            "failed_count": len(body.trades) - inserted_count
        }

    except HTTPException:
        raise
    except Exception as e:
        import logging
        logging.error(f"bulk-trades error: {e}")
        raise HTTPException(status_code=500, detail=f"Bulk insert failed: {str(e)}")
