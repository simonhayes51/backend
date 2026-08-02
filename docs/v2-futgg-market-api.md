# FUT.GG-backed v2 market API

New, additive endpoints under `/api/v2` (registered in
`app/routers/v2/__init__.py`, implemented in
`app/routers/v2/futgg_market.py`) that surface a provider-neutral
market-intelligence layer built entirely on FUT.GG data
(`futgg_players` / `futgg_bin_history` / `futgg_sales_history`, scraped
by the sibling `auto_sync` repo, aggregated into the
`futgg_market_snapshot` materialized view - see
`migrations/038_futgg_market_layer.sql`).

This is a **separate data path from the existing FUTBIN-backed v1/v2
surface** (`fut_players`, `bin_history`, `sales_history`,
`fair_value_mv`, `/api/v2/players/{id}/summary`, `/api/market/fair-value/*`,
etc.) - nothing below replaces or reads from those. `card_source_map`
exists for a future FUTGG↔FUTBIN identity mapping but is not yet
populated, so today the two surfaces are not cross-referenced.

Not gated behind any subscription tier today (see the router's own
module docstring for why).

## Data semantics that shape the contract

- **BIN price** (`current_bin` and everything derived from it) is only
  ever the FUT.GG scraper's most recent observed lowest-BIN listing. If
  there is no observation, every price field is `null` - never `0` and
  never fabricated from sales data alone.
- **Sales timestamps are approximate.** `futgg_sales_history.approximate_sold_at`
  is derived from a relative age string ("4 minutes ago") captured at
  scrape time, not an exact EA transaction time. Every sales-row
  response below carries an explicit `"approximate": true` flag and an
  `age_text` alongside `approximate_sold_at` - never present that field
  as exact.
- **Untradeable cards** (`is_tradeable = false`, i.e. SBC/objective
  rewards) always resolve to `signal: "avoid"` / `risk_level: "avoid"`
  and never get a fair value - they are not live market targets.
- **`insufficient_data`** is returned instead of any buy/sell signal
  whenever: the recent-sales sample has fewer than 5 rows, the BIN
  observation is missing or older than 120 minutes, or the sales
  dispersion ratio (stddev / median) is >= 0.45. See
  `app/services/futgg_intelligence.py` for the exact thresholds and
  their rationale.

## `intelligence` object shape

Returned inline wherever a per-card recommendation applies
(`/players/{id}`, and every row of `/players` when any intelligence
filter is used, `/opportunities`, `/trade-finder`):

```jsonc
{
  "fair_value": 51200,                 // int or null
  "recommended_buy_max": 47000,        // int or null
  "recommended_sell_target": 51000,    // int or null, valid EA increment
  "expected_profit_after_tax": 1234.5, // Decimal->float, via trading_math.net_profit
  "expected_roi": 0.041,               // Decimal->float, via trading_math.net_roi
  "liquidity_score": 0.62,             // 0..1 or null
  "confidence_score": 0.71,            // 0..1
  "risk_level": "low",                 // low|medium|high|avoid
  "signal": "buy",                     // strong_buy|buy|watch|hold|sell|avoid|insufficient_data
  "signal_reasons": ["Current BIN is 8.2% below the median of 42 recent sales.", "..."],
  "price_age_minutes": 4,
  "sales_sample_size": 42,
  "sales_window_span_minutes": 358.0
}
```

## Endpoints

All paths below are relative to `/api/v2`.

### `GET /players`
Search/list. Query params: `search`, `rating_min`, `rating_max`,
`position`, `rarity`, `club`, `league`, `nation`, `max_price`,
`min_price`, `max_price_age_minutes`, `risk`, `min_expected_profit`,
`min_roi`, `min_confidence`, `min_liquidity`, `page`, `page_size`
(default 25, max 100). Returns `{"items": [...], "page", "page_size", "total"}`;
each item includes `intelligence` only when at least one
intelligence-derived filter (`risk`/`min_expected_profit`/`min_roi`/
`min_confidence`/`min_liquidity`) is supplied - a plain listing query
skips per-row scoring entirely.

### `GET /players/{card_id}`
Full detail: `{"source": "futgg", "player": {...snapshot fields...}, "intelligence": {...}}`.
404 if the card has no `futgg_market_snapshot` row.

### `GET /players/{card_id}/prices`
BIN history from `futgg_bin_history`. Query params: `period`
(`1h|6h|24h|1d|7d|14d|30d`, omit for full history), `page`, `page_size`
(default 50, max 200).

### `GET /players/{card_id}/sales`
Recent completed sales from `futgg_sales_history`. Query param: `limit`
(default/max 50). Every row carries `age_text`, `approximate_sold_at`,
`approximate: true`; response also includes a top-level `note` restating
the approximation.

### `GET /opportunities`
Cards where `intelligence.signal` is `buy` or `strong_buy`, sorted
strong_buy-first then by descending expected ROI. Same filter set as
`/players` plus `min_profit`, `min_roi`, `min_confidence`, `min_liquidity`.

### `GET /trade-finder`
Same shape as `/opportunities` with a fuller filter/sort set: `budget`,
`min_profit`, `min_roi`, `risk_tolerance` (`low|medium|high`),
`min_confidence`, `position`, `rarity`, `rating_min`, `rating_max`,
`min_liquidity`, `max_price_age_minutes`, and `sort_by` (one of
`best_opportunity|profit|roi|confidence|liquidity|newest|freshest`,
default `best_opportunity`; 400 on an unrecognized value).

### `GET /market/freshness`
Worker heartbeat / freshness summary, reading `pipeline_heartbeats` for
the `futgg_player_sync` and `futgg_price_sync` workers plus aggregate
counts from `futgg_market_snapshot`:
`{"heartbeats": [...], "cards_discovered", "cards_priced", "cards_no_market", "cards_untradeable", "cards_stale", "stale_by_price_tier": {...}, "latest_source_errors": [...]}`.

## Operational notes

- `futgg_market_snapshot` is a materialized view, refreshed via
  `REFRESH MATERIALIZED VIEW CONCURRENTLY` by
  `app/services/market_data_provider.py::refresher_loop`, wired into
  `main.py`'s lifespan (`FUTGG_MARKET_SNAPSHOT_REFRESH_SECONDS`, default
  120s poll, watermark-gated off `futgg_players.last_seen_at`/
  `price_updated_at` so it's a no-op when nothing has changed).
- Candidate scans for `/opportunities` and `/trade-finder` are capped at
  `CANDIDATE_SCAN_LIMIT = 500` rows (see the router module) since
  intelligence scoring happens in Python, not SQL - a request's cost is
  bounded regardless of table size, at the cost of only ever considering
  the top 500 matching rows per request.
