# app/services/deal_confidence.py
#
# A 7-factor weighted "deal confidence" score (momentum, regime agreement,
# volatility risk, liquidity, spread proxy, support/resistance room, catalyst
# timing). This module used to be dead code with a broken call
# (get_price_history(player_id=...), but that function's actual parameter is
# card_id - would raise TypeError if ever invoked) while the real, live math
# ran duplicated inline in main.py's /api/deal-confidence/{card_id} route.
# This is now that same live math, moved here verbatim (fixing the parameter
# name in the process) so main.py can delegate to one canonical
# implementation instead of maintaining two copies.
from __future__ import annotations

from typing import Any, Dict

from app.services.price_history import get_price_history
from app.services.prices import get_player_price
from app.utils.timebox import next_daily_london_hour, now_utc


def _slope(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    xbar = (n - 1) / 2.0
    ybar = sum(xs) / n
    num = sum((i - xbar) * (y - ybar) for i, y in enumerate(xs))
    den = sum((i - xbar) ** 2 for i in range(n))
    return num / den if den else 0.0


async def compute_deal_confidence(card_id: int, platform: str = "ps") -> Dict[str, Any]:
    # get_price_history() returns {"points": [...]}, not a bare list (see
    # app/routers/price_history.py, which returns its result to callers
    # unchanged) - the inline main.py version this was extracted from
    # iterated the dict itself ("for p in hist"), which iterates its keys
    # (just the string "points") and crashes on the first .get() call
    # below. So this route 500'd on every real call in production, not
    # just "unreachable dead code" - fixed here as part of the extraction.
    hist = (await get_price_history(card_id, platform, "today")).get("points", [])
    prices = [
        p.get("price") or p.get("v") or p.get("y")
        for p in hist
        if (p.get("price") or p.get("v") or p.get("y"))
    ]
    if len(prices) < 6:
        live_price = await get_player_price(card_id, platform)
        if not live_price:
            return {"score": 0, "components": {}, "note": "no data"}
        prices = [int(live_price)] * 6

    n = len(prices)
    last_q = prices[max(0, n - max(6, n // 4)):]
    slope = _slope(last_q)
    momentum4h = 1.0 if slope > 0 else 0.0

    first = prices[:n // 2] or prices
    second = prices[n // 2:] or prices
    regime_agreement = 1.0 if (sum(second) / len(second) >= sum(first) / len(first)) else 0.0

    diffs = [abs(prices[i] - prices[i - 1]) for i in range(1, n)]
    vol_abs = sum(diffs) / len(diffs) if diffs else 0.0
    avg_price = sum(prices) / len(prices)
    vol_risk = min(1.0, (vol_abs / avg_price) if avg_price else 1.0)

    liquidity = min(1.0, max(0.0, (n - 6) / 90))

    window = prices[-min(12, n):]
    if window:
        lo, hi = min(window), max(window)
        spread_proxy = (hi - lo) / hi if hi else 0.1
    else:
        spread_proxy = 0.1

    recent_hi = max(window) if window else max(prices)
    cur = prices[-1]
    sr_room = (recent_hi - cur) / recent_hi if recent_hi else 0.0

    secs = (next_daily_london_hour(18) - now_utc()).total_seconds()
    catalyst_boost = max(0.0, min(1.0, 1 - abs(secs) / (6 * 3600)))

    score = 100 * (
        0.22 * momentum4h
        + 0.14 * regime_agreement
        + 0.16 * (1 - vol_risk)
        + 0.18 * liquidity
        + 0.12 * (1 - spread_proxy)
        + 0.10 * sr_room
        + 0.08 * catalyst_boost
    )
    score = max(0.0, min(100.0, score))
    return {
        "score": round(score, 1),
        "components": {
            "momentum4h": round(momentum4h, 3),
            "regimeAgreement": regime_agreement,
            "volRisk": round(vol_risk, 3),
            "liquidity": round(liquidity, 3),
            "spreadProxy": round(spread_proxy, 3),
            "srRoom": round(sr_room, 3),
            "catalystBoost": round(catalyst_boost, 3),
        },
    }
