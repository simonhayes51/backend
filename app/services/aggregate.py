# app/services/aggregate.py
from __future__ import annotations
from datetime import datetime, timezone
from collections import defaultdict
from typing import Dict, Tuple

# timeframe -> bucket size (seconds)
FRAME_S = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


async def aggregate_ticks_to_candles(db, timeframe: str = "15m", since_hours: int = 48) -> Dict[str, int]:
    """
    Build/refresh OHLCV candles in fut_candles for a single timeframe.

    Schema (your migration):
      - fut_ticks(player_card_id TEXT, platform TEXT, ts TIMESTAMPTZ, price INT)
      - fut_candles(player_card_id TEXT, platform TEXT, timeframe TEXT, open_time TIMESTAMPTZ,
                    open INT, high INT, low INT, close INT, volume INT)
    """
    if timeframe not in FRAME_S:
        raise ValueError(f"Unsupported timeframe: {timeframe}")
    sec = FRAME_S[timeframe]

    # Pull recent ticks (limit the window so the job stays fast)
    rows = await db.fetch(
        """
        SELECT player_card_id, platform, ts, price
        FROM fut_ticks
        WHERE ts >= NOW() - ($1 || ' hours')::interval
        ORDER BY player_card_id, platform, ts ASC
        """,
        since_hours,
    )
    if not rows:
        return {"updated": 0}

    # Group ticks by (player, platform, bucket)
    buckets: Dict[Tuple[str, str, int], list] = defaultdict(list)
    for r in rows:
        ts = r["ts"].replace(tzinfo=timezone.utc)
        epoch = int(ts.timestamp())
        bucket = epoch - (epoch % sec)  # floor to timeframe
        key = (r["player_card_id"], r["platform"], bucket)
        buckets[key].append(r["price"])

    updated = 0
    for (player_card_id, platform, bucket), prices in buckets.items():
        if not prices:
            continue
        open_px = prices[0]
        close_px = prices[-1]
        high_px = max(prices)
        low_px = min(prices)
        vol = len(prices)  # proxy volume = # of ticks
        open_time = datetime.fromtimestamp(bucket, tz=timezone.utc)

        await db.execute(
            """
            INSERT INTO fut_candles (player_card_id, platform, timeframe, open_time, open, high, low, close, volume)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
            ON CONFLICT (player_card_id, platform, timeframe, open_time)
            DO UPDATE SET open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                          close=EXCLUDED.close, volume=EXCLUDED.volume
            """,
            player_card_id, platform, timeframe, open_time, open_px, high_px, low_px, close_px, vol
        )
        updated += 1

    return {"updated": updated}


async def aggregate_all_timeframes(db, since_hours: int = 48) -> Dict[str, Dict[str, int]]:
    """
    Aggregate all default timeframes. Returns a small stat dict per timeframe.
    """
    out = {}
    for tf in ("5m", "15m", "1h", "4h", "1d"):
        out[tf] = await aggregate_ticks_to_candles(db, tf, since_hours)
    return out
