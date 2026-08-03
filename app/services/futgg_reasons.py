# app/services/futgg_reasons.py
"""
Structured reason codes for every rejection, downgrade, or supporting
observation the FUT.GG engine emits.

The engine previously returned only free-text reason strings. Those are
good for a user and useless for anything else: you cannot group by them,
you cannot count how often "unresolved downtrend" blocked a buy last
week, and you certainly cannot ask the outcome grader "did the calls we
downgraded for excessive dispersion actually go on to lose money".

So every reason is now a (code, message) pair:

  * `code` is a stable machine-readable identifier. Stable is the
    operative word - these are persisted onto recommendation snapshots
    and grouped by in the track-record API, so renaming one is a
    breaking change to historical data, not a cosmetic edit.
  * `message` is the user-facing English, built at call time so it can
    carry the actual numbers.

Codes are grouped by what they do to a recommendation:
  BLOCK_*   - prevents any actionable signal
  DOWNGRADE_* - allows a signal but caps or reduces it
  INFO_*    - supporting evidence, no effect on the decision
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


# ---- Blocking codes (no actionable buy signal) ------------------------------
INSUFFICIENT_SALES = "insufficient_sales"
STALE_MARKET = "stale_market"
NO_LIVE_PRICE = "no_live_price"
UNTRADEABLE = "untradeable"
UNRESOLVED_DOWNTREND = "unresolved_downtrend"
FALLING_KNIFE = "falling_knife"
EXPECTED_PROFIT_TOO_LOW = "expected_profit_too_low"
TARGET_BELOW_BREAK_EVEN = "target_below_break_even"
NO_REALISTIC_EXIT_EVIDENCE = "no_realistic_exit_evidence"
PRICE_ABOVE_MAX_BUY = "price_above_max_buy"
EVENT_RISK = "event_risk"
STALE_RECOMMENDATION = "stale_recommendation"

# ---- Downgrading codes (signal allowed but capped) --------------------------
EXCESSIVE_DISPERSION = "excessive_dispersion"
LOW_LIQUIDITY = "low_liquidity"
LOW_CONFIDENCE = "low_confidence"
UNRELIABLE_ENTRY = "unreliable_entry"
TREND_UNCONFIRMED = "trend_unconfirmed"
THIN_SALES_WINDOW = "thin_sales_window"
ELEVATED_EVENT_RISK = "elevated_event_risk"

# ---- Informational ----------------------------------------------------------
INFO_BIN_VS_MEDIAN = "bin_vs_median"
INFO_SALES_WINDOW = "sales_window"
INFO_PRICE_AGE = "price_age"
INFO_DISPERSION = "dispersion"
INFO_TREND_STATE = "trend_state"
INFO_ROI_CLEARS_BUY = "roi_clears_buy"
INFO_ROI_CLEARS_STRONG_BUY = "roi_clears_strong_buy"
INFO_WATCH_THRESHOLD = "watch_threshold"
INFO_NO_EDGE = "no_edge"

BLOCKING_CODES = frozenset({
    INSUFFICIENT_SALES, STALE_MARKET, NO_LIVE_PRICE, UNTRADEABLE,
    UNRESOLVED_DOWNTREND, FALLING_KNIFE, EXPECTED_PROFIT_TOO_LOW,
    TARGET_BELOW_BREAK_EVEN, NO_REALISTIC_EXIT_EVIDENCE,
    PRICE_ABOVE_MAX_BUY, EVENT_RISK, STALE_RECOMMENDATION,
})

DOWNGRADE_CODES = frozenset({
    EXCESSIVE_DISPERSION, LOW_LIQUIDITY, LOW_CONFIDENCE, UNRELIABLE_ENTRY,
    TREND_UNCONFIRMED, THIN_SALES_WINDOW, ELEVATED_EVENT_RISK,
})


@dataclass(frozen=True)
class Reason:
    code: str
    message: str

    @property
    def is_blocking(self) -> bool:
        return self.code in BLOCKING_CODES

    @property
    def is_downgrade(self) -> bool:
        return self.code in DOWNGRADE_CODES

    def as_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message}


class ReasonList:
    """Ordered, append-only collection of Reasons with convenience views.

    Kept as a tiny class rather than a bare list so callers can ask
    `reasons.blocking_codes` without every call site re-deriving it, and
    so the legacy `List[str]` shape (`signal_reasons`) stays trivially
    available for the existing API contract."""

    def __init__(self) -> None:
        self._items: List[Reason] = []

    def add(self, code: str, message: str) -> None:
        self._items.append(Reason(code, message))

    def extend(self, other: "ReasonList") -> None:
        self._items.extend(other._items)

    @property
    def items(self) -> List[Reason]:
        return list(self._items)

    @property
    def messages(self) -> List[str]:
        """The legacy free-text shape the existing API already returns."""
        return [r.message for r in self._items]

    @property
    def codes(self) -> List[str]:
        return [r.code for r in self._items]

    @property
    def blocking_codes(self) -> List[str]:
        return [r.code for r in self._items if r.is_blocking]

    @property
    def downgrade_codes(self) -> List[str]:
        return [r.code for r in self._items if r.is_downgrade]

    def has(self, code: str) -> bool:
        return any(r.code == code for r in self._items)

    def as_dicts(self) -> List[Dict[str, str]]:
        return [r.as_dict() for r in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)


# Human-readable fallbacks, used where a code needs to be rendered
# without the original numbers (e.g. a track-record breakdown grouping by
# reason code, where there is no single card's numbers to quote).
CODE_LABELS: Dict[str, str] = {
    INSUFFICIENT_SALES: "Not enough recent sales",
    STALE_MARKET: "Price data too old",
    NO_LIVE_PRICE: "No live listing price",
    UNTRADEABLE: "Card is untradeable",
    UNRESOLVED_DOWNTREND: "Still falling",
    FALLING_KNIFE: "Falling knife",
    EXPECTED_PROFIT_TOO_LOW: "Profit too small",
    TARGET_BELOW_BREAK_EVEN: "Target below break-even",
    NO_REALISTIC_EXIT_EVIDENCE: "No evidence it sells at target",
    PRICE_ABOVE_MAX_BUY: "Costs more than it is worth buying at",
    EVENT_RISK: "Known market event risk",
    STALE_RECOMMENDATION: "Recommendation out of date",
    EXCESSIVE_DISPERSION: "Sale prices too scattered",
    LOW_LIQUIDITY: "Trades rarely",
    LOW_CONFIDENCE: "Low confidence",
    UNRELIABLE_ENTRY: "Entry price unreliable",
    TREND_UNCONFIRMED: "Trend not yet confirmed",
    THIN_SALES_WINDOW: "Thin sales window",
    ELEVATED_EVENT_RISK: "Elevated event risk",
    INFO_BIN_VS_MEDIAN: "Price versus recent sales",
    INFO_SALES_WINDOW: "Sales window",
    INFO_PRICE_AGE: "Price age",
    INFO_DISPERSION: "Sales dispersion",
    INFO_TREND_STATE: "Market trend",
    INFO_ROI_CLEARS_BUY: "Clears buy threshold",
    INFO_ROI_CLEARS_STRONG_BUY: "Clears strong-buy threshold",
    INFO_WATCH_THRESHOLD: "Watch threshold",
    INFO_NO_EDGE: "No edge",
}


def label_for(code: str) -> str:
    return CODE_LABELS.get(code, code.replace("_", " ").capitalize())
