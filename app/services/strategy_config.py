"""
Strategy policies for Recommendation Engine V1.2 - the configurable
per-strategy BUY thresholds. There is deliberately no single global BUY
threshold anywhere in this codebase (see recommendation_engine_v2.py) -
every strategy is evaluated independently against its own policy here.

FLAGGED: these are documented starting defaults, not values tuned
against real outcome data - there is no closed-label history to tune
against yet (see migrations/024's ml_labels table). Revisit once labels
accumulate.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional


@dataclass(frozen=True)
class StrategyPolicy:
    name: str
    min_likely_net_roi: Decimal
    min_conservative_net_roi: Decimal
    min_liquidity: Optional[float] = None
    min_confidence: Optional[float] = None
    max_risk: Optional[float] = None
    momentum_floor: Optional[float] = None
    require_positive_momentum: bool = False


STRATEGY_POLICIES: Dict[str, StrategyPolicy] = {
    "quick_flip": StrategyPolicy(
        name="quick_flip",
        min_likely_net_roi=Decimal("0.03"),
        min_conservative_net_roi=Decimal("-0.01"),
        min_liquidity=0.50,
        min_confidence=0.50,
        max_risk=0.50,
        momentum_floor=-0.10,
    ),
    "swing_trade": StrategyPolicy(
        name="swing_trade",
        min_likely_net_roi=Decimal("0.06"),
        min_conservative_net_roi=Decimal("-0.03"),
        min_liquidity=0.35,
        min_confidence=0.50,
        max_risk=0.55,
        momentum_floor=-0.20,
    ),
    "long_hold": StrategyPolicy(
        name="long_hold",
        min_likely_net_roi=Decimal("0.10"),
        min_conservative_net_roi=Decimal("-0.05"),
        min_confidence=0.55,
        max_risk=0.50,
        require_positive_momentum=True,
    ),
    "low_risk": StrategyPolicy(
        name="low_risk",
        min_likely_net_roi=Decimal("0.04"),
        # "conservative ROI must be non-negative" - encoded directly as
        # the floor itself, not a separate flag.
        min_conservative_net_roi=Decimal("0.00"),
        min_liquidity=0.40,
        min_confidence=0.65,
        max_risk=0.30,
    ),
}

# lazy_buyer has a different shape (discount-based, not a plain ROI
# floor) - kept as its own small config rather than force-fit into
# StrategyPolicy's fields.
LAZY_BUYER_MIN_DISCOUNT_VS_LIKELY = Decimal("0.05")
LAZY_BUYER_MIN_DISCOUNT_VS_FAIR_VALUE = Decimal("0.10")
LAZY_BUYER_MIN_LIQUIDITY = 0.30
LAZY_BUYER_MIN_NET_ROI = Decimal("0.03")  # "must clear the configured lazy-buyer margin after tax"

# Minimum confidence floor gating ANY recommendation at all (below this,
# status is INSUFFICIENT_DATA / LOW_CONFIDENCE regardless of which
# strategy might otherwise look attractive) - distinct from and lower
# than any individual strategy's own min_confidence, since a strategy's
# threshold is "good enough to act on", this one is "good enough to even
# have an opinion".
MIN_DECISION_CONFIDENCE = 0.35

MAX_ACCEPTABLE_STALENESS_MINUTES = 60


def validate_policies() -> None:
    for policy in STRATEGY_POLICIES.values():
        if policy.min_liquidity is not None and not (0.0 <= policy.min_liquidity <= 1.0):
            raise ValueError(f"{policy.name}: min_liquidity out of [0,1]")
        if policy.min_confidence is not None and not (0.0 <= policy.min_confidence <= 1.0):
            raise ValueError(f"{policy.name}: min_confidence out of [0,1]")
        if policy.max_risk is not None and not (0.0 <= policy.max_risk <= 1.0):
            raise ValueError(f"{policy.name}: max_risk out of [0,1]")
        if policy.momentum_floor is not None and not (-1.0 <= policy.momentum_floor <= 1.0):
            raise ValueError(f"{policy.name}: momentum_floor out of [-1,1]")


validate_policies()
