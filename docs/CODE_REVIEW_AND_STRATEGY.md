# FUT Trading Platform — Full Code Review & Product Strategy

_Covers all three repos: `auto_sync` (data pipeline), `backend` (FastAPI API), `frontend_new` (React/Vite app). Reviewed July 2026._

---

## 1. Executive Summary

**What you have:** a genuinely differentiated data asset wrapped in an under-hardened application. The `sales_history` table (real, timestamped completed sales scraped every 10 minutes) plus `bin_history` (10-minute lowest-BIN timeline) is data that FUTBIN and FUT.GG **do not expose to anyone via API**. The public paid API (`/api/public/v1/*`) built on top of it is the single most defensible thing in the codebase. The trade journal + watchlist + alerting core is a solid retention engine.

**What's blocking you:**

1. **Monetization is switched off in code.** `compute_entitlements()` grants every logged-in user elite tier, and `require_feature()` returns `True` unconditionally. Stripe billing works, but pays for nothing — every "premium" feature is free right now.
2. **Several exploitable security holes** (unauthenticated debug endpoint leaking session cookies, stack traces returned to clients, forgeable extension JWTs under default config).
3. **The "AI" features are not AI** — canned keyword-matched strings. That's a churn and reputation risk the moment a paying user pokes at it.
4. **The richest market segment (promo/special cards) has no price history** — the history worker only tracks ordinary golds.
5. **Zero automated tests, no migrations discipline, no monitoring** — the watchlist alert loop was silently dead for a period because of a `NameError` (documented in the code itself). Nothing would catch that today.

**The one-line strategy:** stop competing with FUTBIN on being a price-lookup site, and become the **Bloomberg Terminal of FUT** — the only platform with real completed-sales data, a real API, real analytics on top of it, and a trade journal that proves (or disproves) every recommendation it makes.

---

## 2. Code Review

### 2.1 Architecture overview

```
auto_sync (Railway workers)
 ├── futbin_full_sync.py      daily full-catalog crawl (848 pages) → fut_players
 ├── bin_sales_history_sync.py 10-min BIN + sales scrape (gold rares) → bin_history, sales_history
 ├── ea_price_sync.py          EA UT API price sync using x-ut-sid session token  ⚠️
 └── bot.py                    Discord bot shell (no cogs loaded — dead)

backend (FastAPI, asyncpg, 3 pools)
 ├── main.py                   3,894-line monolith: OAuth, sessions, trades CRUD,
 │                             billing, webhooks, watchlist alert loop, momentum scraping
 ├── app/routers/*             15 routers (players, trade_finder, smart_buy, ai_engine,
 │                             portfolio, leaderboard, referrals, public_api, api_keys…)
 ├── app/services/*            price_history, trade_finder scoring, indicators, seasonality
 └── migrations/*.sql          12 SQL files with no runner (schema also created at runtime)

frontend_new (React 18 + Vite + Tailwind, HashRouter, PWA-ish)
 └── ~30 pages, lazy-loaded, context-based state, axios with retry/429 handling
```

### 2.2 Strengths (keep these)

- **The data pipeline design is smart.** Extracting the fut.gg/EA `card_id` from futbin's image URLs gives exact cross-source identity with no fuzzy matching. Append-only `bin_history`/`sales_history` with a natural-key dedupe is the right shape for a timeline.
- **Scraping discipline:** 429-aware backoff with `Retry-After` honoring, bounded concurrency (3), politeness delays, column auto-detection before upsert, NOT-NULL preflight checks. The inline comments documenting *verified* page-markup findings are genuinely excellent engineering hygiene.
- **API key system done right:** SHA-256-hashed keys, show-once plaintext, prefix display, revocation, per-key rate limits.
- **Statistical honesty in scoring:** IQR + MAD outlier trimming, median bucketing, refusal to fabricate signals when history is empty (`top_buys` skips cards with no real data rather than inventing a score).
- **Watchlist alerts are thoughtfully specced:** quiet hours, cool-off, DM-with-channel-fallback, price *and* liquidity (sales/hour) metrics, three reference modes.
- **Frontend fundamentals:** lazy routes with Suspense, ErrorBoundary, manual vendor chunking, an axios instance with exponential-backoff retries, Retry-After honoring, and a 402 → `premium:blocked` event pattern for upsells.
- **Entitlements design** (tier matrix, per-tier limits) is well-modeled — it's just deliberately bypassed (see below).

### 2.3 Critical issues (fix before anything else)

| # | Severity | Issue | Location |
|---|----------|-------|----------|
| 1 | 🔴 Revenue | All authenticated users get elite tier free; `require_feature()` is a no-op | `backend/app/auth/entitlements.py:196-233` |
| 2 | 🔴 Security | `/api/debug/session-state` returns session contents **and all cookies** to any caller, unauthenticated | `backend/main.py:3857-3865` |
| 3 | 🔴 Security | Global exception handlers return `str(exc)` + **full traceback** in the HTTP response body | `backend/main.py:917-933`, duplicated at `main.py:3852-3855` |
| 4 | 🔴 Security | Extension JWT falls back to `JWT_PRIVATE_KEY="dev-secret-change-me"` — if that env var is unset in prod, anyone can mint valid `trade:ingest` tokens for any Discord ID | `backend/main.py:87,1081-1098` |
| 5 | 🔴 Security | Dashboard OAuth flow never validates `state` (only the extension flow does) → login CSRF | `backend/main.py:1278-1330` |
| 6 | 🔴 ToS/ban | `ea_price_sync.py` calls EA's UT transfer-market API with a personal `x-ut-sid` web-app token. This violates EA ToS and risks a permanent ban of the EA account used — and transfer-market bans have historically been applied aggressively to automated callers | `auto_sync/ea_price_sync.py` |
| 7 | 🟠 Correctness | `UndefinedTableError` exception handler returns **HTTP 200** with an error string — clients treat failed responses as success | `backend/main.py:843-848` |
| 8 | 🟠 Reliability | Schema is created/altered at app startup inside `lifespan()` (dozens of `CREATE TABLE IF NOT EXISTS`/`ALTER TABLE`), while `migrations/*.sql` has no runner. Two competing sources of schema truth; startup DDL races if you ever run >1 instance | `backend/main.py:430-822` |
| 9 | 🟠 Scale | All coordination state is in-process: `OAUTH_STATE` dict, price/momentum caches, role cache, API rate limiter. Running 2+ workers breaks OAuth handoff and makes rate limits per-process | `main.py:104`, `app/auth/api_keys.py:43` |
| 10 | 🟠 Quality | **Zero tests** in all three repos (vitest configured, no test files; no pytest anywhere), no CI, no error tracking (no Sentry), workers log via `print()` | everywhere |

**The proof that #10 matters is in your own code:** `main.py:1100-1105` documents that `fetch_price()` was called from three places but *never defined* — the watchlist alert loop crashed with `NameError` on every poll, so **price alerts never fired** until someone noticed. A single smoke test or Sentry alert would have caught that on day one.

### 2.4 High-priority issues

- **Duplicate/dead code everywhere:** a stale 2,802-line `main` file next to `main.py`; `app/routes/` vs `app/routers/` both live; two axios instances with different behavior (`src/axios.js` vs `src/utils/axios.js`); `pwa.js` contains a pasted-in React `TradingGoals` component with missing imports (crashes if ever imported); stray `.txt` placeholder files.
- **Two pool systems:** `lifespan()` creates pools on `app.state` while `app/db.py` lazily creates a *second* set of pools for the same databases. Up to 6 pools × 10 connections against Railway Postgres.
- **Hot paths scrape futbin live per request:** `/api/fut-player-price/{id}` does a live page fetch + a `/market` fetch + a sales-page fetch per call (3 upstream HTML requests); `/api/