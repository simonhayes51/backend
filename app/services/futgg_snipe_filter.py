# app/services/futgg_snipe_filter.py
"""
Turns a recommendation into something a user can actually execute.

WHY THIS EXISTS
---------------
The product's core loop was broken by latency it could not fix by
scraping harder. A user reads "Haaland is underpriced", switches to the
Web App, types the name, sets filters, and by then the listing is gone.
Even a ten-minute-old tip is unusable if acting on it takes two minutes
of manual setup.

A snipe filter closes that gap without touching scrape speed: instead of
a claim about a card, the user gets the exact search to run - name,
quality, position, and a maximum BIN. That converts "this was cheap
recently" into "put this in your search and let the market come to you",
which is the only form of a price tip that survives contact with a
market that moves faster than a human can.

THE MAXIMUM BIN IS THE PROFITABLE THRESHOLD, NOT THE CURRENT PRICE
------------------------------------------------------------------
This is the point of the whole module. Setting max BIN to the current ask
would be pointless - it just reproduces what is already listed. The
filter's max BIN is `recommended_buy_max`: the highest price at which the
trade still works. A user running that filter buys only at prices that
are actually profitable, whether or not anything is listed there right
now. It is the difference between chasing a price and setting a trap.

Which is also why a card currently ABOVE its buy threshold still gets a
filter - as a watch condition. "Buy only if it falls to X" is directly
executable as a standing search; "this card is currently too expensive"
is not.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Optional

from app.services.futgg_intelligence import CardIntelligence

# EA's Web App search exposes quality/rarity as a coarse filter. Mapping
# FUT.GG's rarity strings onto it is deliberately conservative: an
# unrecognised rarity yields None (no quality filter) rather than a guess,
# because a wrong quality filter silently returns zero results and the
# user has no way to tell it was our error.
_QUALITY_BY_RARITY = {
    "bronze": "Bronze", "bronze_rare": "Bronze",
    "silver": "Silver", "silver_rare": "Silver",
    "gold": "Gold", "gold_common": "Gold", "gold_rare": "Gold",
}

# Rarities where the Web App's "Rare" toggle is meaningful.
_RARE_RARITIES = {"bronze_rare", "silver_rare", "gold_rare"}


@dataclass
class SnipeFilter:
    """An executable Web App search instruction."""

    player_name: str
    card_id: int
    # "buy" - run this now, it is currently profitable at this price.
    # "watch" - run this as a standing search; it triggers if the price
    #           falls to max_bin.
    mode: str
    max_bin: int
    version: Optional[str] = None
    position: Optional[str] = None
    quality: Optional[str] = None
    is_rare: Optional[bool] = None
    rating: Optional[int] = None

    recommended_quantity: int = 1
    target_sell_price: Optional[int] = None
    break_even_price: Optional[int] = None
    min_acceptable_roi_pct: Optional[float] = None
    expected_hold_label: Optional[str] = None
    expires_at: Optional[datetime] = None

    #: Single-line instruction, ready to display or copy.
    instruction: str = ""

    def as_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        out["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        return out


def _quantity_for(liquidity: Optional[float], confidence: Optional[float]) -> int:
    """How many to buy.

    Deliberately conservative and capped low. Position sizing on an
    illiquid card is the fastest way to turn a good call into a bag you
    cannot exit - you can only sell as fast as the market absorbs, and
    the engine has no view on the depth of the order book, only on
    recent throughput.
    """
    if liquidity is None or confidence is None:
        return 1
    if liquidity >= 0.7 and confidence >= 0.7:
        return 3
    if liquidity >= 0.5 and confidence >= 0.55:
        return 2
    return 1


def _hold_label(liquidity: Optional[float]) -> str:
    """Expected hold, phrased as a range and grounded in observed
    throughput rather than a prediction. A card that trades constantly
    clears quickly; a thin one does not."""
    if liquidity is None:
        return "Unknown"
    if liquidity >= 0.7:
        return "Minutes to a few hours"
    if liquidity >= 0.45:
        return "A few hours to a day"
    if liquidity >= 0.2:
        return "1-3 days"
    return "Several days or longer"


def build_snipe_filter(
    snapshot: Dict[str, Any], ci: CardIntelligence,
) -> Optional[SnipeFilter]:
    """Build an executable filter for a recommendation.

    Returns None when there is nothing actionable to express - an
    untradeable card, or one with no computed buy threshold at all.
    A `watch` still produces a filter (that is the standing-search case);
    only a genuine absence of a threshold produces None.
    """
    if ci.recommended_buy_max is None:
        return None
    if snapshot.get("is_tradeable") is False:
        return None
    if ci.signal not in ("buy", "strong_buy", "watch"):
        return None

    name = snapshot.get("name") or "Unknown player"
    rarity = (snapshot.get("rarity") or "").lower()
    max_bin = int(ci.recommended_buy_max)
    is_buy = ci.current_executable_buy is not None and ci.signal in ("buy", "strong_buy")

    quality = _QUALITY_BY_RARITY.get(rarity)
    is_rare = True if rarity in _RARE_RARITIES else (False if rarity in _QUALITY_BY_RARITY else None)

    if is_buy:
        instruction = (
            f"Search {name}, set max BIN {max_bin:,} - buy anything at or below this."
        )
        mode = "buy"
    else:
        instruction = (
            f"Search {name}, set max BIN {max_bin:,} - do not buy above this. "
            f"Currently {int(snapshot.get('current_bin') or 0):,}."
        )
        mode = "watch"

    return SnipeFilter(
        player_name=name,
        card_id=ci.card_id,
        mode=mode,
        max_bin=max_bin,
        version=snapshot.get("rarity"),
        position=snapshot.get("primary_position"),
        quality=quality,
        is_rare=is_rare,
        rating=snapshot.get("rating"),
        recommended_quantity=_quantity_for(ci.liquidity_score, ci.confidence_score),
        target_sell_price=int(ci.recommended_sell_target) if ci.recommended_sell_target else None,
        break_even_price=int(ci.break_even_price) if ci.break_even_price else None,
        min_acceptable_roi_pct=(
            round(float(ci.expected_roi) * 100, 1) if ci.expected_roi is not None else None
        ),
        expected_hold_label=_hold_label(ci.liquidity_score),
        expires_at=ci.expires_at,
        instruction=instruction,
    )
