# app/services/futgg_lifecycle.py
"""
Recommendation expiry and invalidation.

A recommendation is a claim about a market state. When that state moves,
the claim stops being true - but the numbers attached to it (expected
profit, ROI, buy threshold) remain sitting on screen looking exactly as
authoritative as they did when they were computed. That is the specific
failure this module prevents: a user acting on an expected-profit figure
derived from a price that no longer exists anywhere.

Two distinct concepts, deliberately not merged:

  EXPIRED     - too old to trust. Time passed; we make no claim about
                whether the market moved. The recommendation had a
                computed shelf life (see futgg_intelligence's expiry
                calculation) and it ran out.

  INVALIDATED - known to be wrong. The live BIN has moved materially away
                from the price the recommendation was computed against,
                so we can positively assert the call no longer holds -
                not merely that we are unsure.

The distinction matters to a user: "this is a few hours old, re-check it"
is a very different message from "this is no longer a buy, the price
moved".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from app.services.futgg_config import ENGINE_CONFIG, EngineConfig
from app.services.futgg_intelligence import (
    STATUS_ACTIVE, STATUS_EXPIRED, STATUS_INVALIDATED, STATUS_WATCH,
)

# Reason strings recorded on invalidation, so the UI can explain *why*
# rather than just greying a card out.
INVALIDATED_PRICE_ROSE = "price_rose_above_threshold"
INVALIDATED_PRICE_MOVED = "price_moved_materially"
INVALIDATED_NO_PRICE = "no_live_price"


@dataclass
class LifecycleVerdict:
    status: str
    reason: Optional[str] = None
    message: Optional[str] = None

    @property
    def is_usable(self) -> bool:
        return self.status in (STATUS_ACTIVE, STATUS_WATCH)

    def as_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "reason": self.reason, "message": self.message}


def check_lifecycle(
    *,
    original_status: str,
    evaluated_bin: Optional[int],
    recommended_buy_max: Optional[float],
    expires_at: Optional[datetime],
    live_bin: Optional[int],
    as_of: Optional[datetime] = None,
    config: Optional[EngineConfig] = None,
) -> LifecycleVerdict:
    """Decide whether a previously-computed recommendation still stands.

    Invalidation is checked BEFORE expiry: knowing a call is wrong is
    more informative than knowing it is old, and a recommendation can
    easily be both. Reporting "expired" for something we can positively
    show is invalid would understate what we know.
    """
    cfg = config or ENGINE_CONFIG
    as_of = as_of or datetime.now(timezone.utc)

    # ---- Invalidation ------------------------------------------------
    if live_bin is None:
        return LifecycleVerdict(
            STATUS_INVALIDATED, INVALIDATED_NO_PRICE,
            "There is no live listing for this card any more.",
        )

    if recommended_buy_max is not None and live_bin > float(recommended_buy_max):
        # An active BUY whose price has risen above the threshold is no
        # longer a buy - it is at best a watch, and continuing to show
        # its original expected profit would be straightforwardly false.
        if original_status == STATUS_ACTIVE:
            return LifecycleVerdict(
                STATUS_INVALIDATED, INVALIDATED_PRICE_ROSE,
                f"The price has risen to {live_bin:,}, above the "
                f"{int(recommended_buy_max):,} maximum this recommendation was based on.",
            )

    if evaluated_bin:
        drift = abs(live_bin - evaluated_bin) / evaluated_bin
        if drift >= cfg.invalidation_bin_drift_pct:
            return LifecycleVerdict(
                STATUS_INVALIDATED, INVALIDATED_PRICE_MOVED,
                f"The price has moved {drift * 100:.0f}% since this was calculated "
                f"({evaluated_bin:,} to {live_bin:,}).",
            )

    # ---- Expiry ------------------------------------------------------
    if expires_at is not None:
        expires_at = expires_at.replace(tzinfo=timezone.utc) if expires_at.tzinfo is None else expires_at
        if as_of >= expires_at:
            return LifecycleVerdict(
                STATUS_EXPIRED, None,
                "This recommendation is past its freshness window - re-check before acting.",
            )

    return LifecycleVerdict(original_status)
