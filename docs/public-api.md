# FutHub Public API (v1)

Read-only access to real historical BIN prices and completed sales data
for Gold Rare FUT cards - the same data collected by `bin_history`/
`sales_history`, exposed for external tools (bots, spreadsheets,
dashboards) instead of requiring HTML scraping.

## Getting a key

1. Log in to FutHub (Premium required).
2. `POST /api/api-keys` with your session cookie, optional `{"name": "my bot"}` body.
3. The response includes the plaintext key **once** - store it immediately, it can't be retrieved again.
4. Manage keys with `GET /api/api-keys` (lists prefixes only) and `DELETE /api/api-keys/{id}` (revoke).

## Authenticating

Send the key on every request:

```
X-API-Key: futhub_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

## Rate limits

60 requests/minute per key by default (429 with a plain error body once exceeded).
The limiter is currently per-process - it resets on deploy and doesn't share
state across multiple backend instances.

## Endpoints

All under `/api/public/v1`.

### `GET /players/search`
Query params: `q` (name or card_id substring), `limit` (default 50, max 200).

### `GET /players/{card_id}/bin-history`
Query params: `platform` (`ps`|`xbox`|`pc`, omit for all), `limit` (default 200, max 2000).
Returns lowest-BIN snapshots, most recent first.

### `GET /players/{card_id}/sales-history`
Query params: `limit` (default 50, max 500).
Returns completed sales (PS market only), most recent first: listed price, sold
price, EA tax, net price, sold time.

### `GET /players/{card_id}/market-metrics`
No params. Returns current BIN per platform, median sold price (24h), sample
size, divergence % vs BIN, liquidity (sales/hour), and sold-price volatility -
all derived from real transactions, not asking price.

## Known gaps (not yet built)

- No usage dashboard or per-key request history in the UI yet.
- No webhook/streaming option - polling only.
- Pricing/tiering for API access as a standalone product (distinct from the
  consumer app's Premium subscription) hasn't been decided - key creation is
  currently gated on the same `is_premium` flag as the rest of the app.
