# FUTHub — Full Code Review & Product Strategy

**Prepared:** July 2026
**Scope:** `backend` (FastAPI/Postgres API), `auto_sync` (scrapers/data pipeline), `frontend_new` (React/Vite SPA)
**Author:** Senior full-stack + FUT-trading review

---

## 1. Executive Summary

FUTHub is already **well ahead of the average FUT trading site in one decisive area: it owns a real, timestamped market dataset.** The `auto_sync` pipeline crawls futbin's full ~848-page catalog daily, pulls lowest-BIN prices directly from EA's own transfer-market API, and — most importantly — records `bin_history` (append-only BIN timeline) and `sales_history` (real completed sales with EA tax and net price). That is a genuine, defensible asset. FUTBIN and FUTWIZ largely show *current* price and a smoothed chart; almost nobody exposes **structured, queryable completed-sales history behind an API**. You have already shipped exactly that (`/api/public/v1/...`). This is the crown jewel and most of my strategy recommendations orbit around monetizing it.

The product surface is also broad: trade journal, dashboard, watchlist + Discord alerts, trending, squad builder, smart-buy AI, trade finder, portfolio optimizer, leaderboard, referrals, a public API tier, and Stripe billing. For a small team this is impressive breadth.

**However**, the codebase carries serious risk in three places that I would fix before any growth push:

1. **Security & auth are effectively wide open.** `compute_entitlements()` grants *every* premium feature to *any* logged-in user, and `require_feature()` is a no-op that always returns `True`. Right now Stripe billing exists but **the paywall does not** — anyone who logs in with Discord gets Elite. This is simultaneously your biggest bug and your fastest revenue unlock.
2. **Operational fragility.** A 3,894-line `main.py`, schema created via `CREATE TABLE IF NOT EXISTS` on every boot, error handlers that leak full stack traces to clients, an OAuth flow that skips guild-membership checks, and a debug endpoint that dumps session + cookies. Several latent bugs are called out below (some already flagged in code comments — e.g. the watchlist alert loop that historically NameError'd).
3. **Scraping dependency is single-threaded on futbin + a manually-refreshed EA session token.** The whole data moat rests on `EA_X_UT_SID` being pasted in by hand and on futbin not changing markup or hardening Cloudflare. There is no proxy rotation, no monitoring, no fallback source. When (not if) this breaks, the product goes dark.

**The strategic thesis:** you are sitting on the data that competitors lack, but you are giving away the premium product for free and betting the company on one hand-maintained cookie. Fix the paywall, harden the pipeline, and lean *hard* into the historical-data + prediction angle that your schema uniquely enables. Do that and "the site with the real sales data and the price-prediction engine" is a defensible, monetizable position FUTBIN cannot copy overnight.

**Top 5 immediate actions:**
1. Make `compute_entitlements()` actually read the subscription/tier and enforce it; restore `require_feature()`.
2. Stop returning `traceback.format_exc()` in HTTP 500 bodies; remove `/api/debug/session-state`.
3. Put the EA/futbin scrapers behind health monitoring + alerting and add session-token expiry alerts.
4. Split `main.py` into routers; freeze schema into migrations run once, not on every boot.
5. Ship a price-prediction / "fair value" signal off `sales_history` — it's your differentiator and it's mostly a query away.

---

## 2. Code Review

### 2.1 Architecture overview

| Layer | Stack | Notes |
|---|---|---|
| Frontend | React 18 + Vite 4 + Tailwind, HashRouter, Recharts + lightweight-charts, axios | SPA, PWA-ish, lazy-loaded pages. Mostly `.jsx`, a few stray `.tsx`. |
| Backend | FastAPI + asyncpg (raw SQL), Starlette sessions, Stripe, aiohttp | One giant `main.py` + `app/routers/*` + `app/services/*`. |
| Data pipeline | Standalone Python workers on Railway | `futbin_full_sync` (daily catalog), `ea_price_sync` (EA BIN), `bin_sales_history_sync` (10-min BIN+sales timeline), `bot.py` (Discord). |
| DB | Postgres, up to 3 logical DBs (core / player / watchlist) via separate DSNs | Schema partly in migrations, partly created at runtime in `lifespan`. |
| Auth | Discord OAuth → Starlette signed-cookie session; JWT (HS256) for the browser extension | Entitlements currently bypassed. |
| Payments | Stripe Checkout + webhooks + Discord role sync | Plumbing is there; enforcement is not. |

The **separation into three logical databases** (core, player, watchlist via `PLAYER_DATABASE_URL` etc.) is a genuinely good decision — it isolates the high-write scraping data from user data and lets you scale the read-heavy player DB independently. Keep that.

### 2.2 Strengths (give credit where due)

- **Real market data model.** `bin_history` (append-only) + `sales_history` (listed/sold/tax/net/timestamp, deduped by a natural key) is exactly right. This is better data than most competitors expose.
- **Pragmatic, resilient scrapers.** `futbin_full_sync.py` auto-detects columns, adds missing ones, checks for unhandled NOT NULL columns *before* the crawl instead of failing row-by-row, and rate-limits politely (1.5s). `bin_sales_history_sync.py` has 429-aware backoff. The comments document *why* each decision was made (e.g. base-card image URL regex, slug mismatch on the sales page). This is unusually thoughtful scraping code.
- **Sensible caching layers** — momentum pages, market summary, price history all have short TTL caches to shield upstreams.
- **API-key tier is well built.** SHA-256 hashing (correct for high-entropy tokens), prefix storage, revocation, per-key rate limiting, show-once plaintext. `app/auth/api_keys.py` is the cleanest module in the repo.
- **Frontend performance basics done right** — route-level `lazy()`/`Suspense`, manual vendor chunks in Vite, an axios wrapper with idempotent GET retries, `Retry-After` handling, and a `premium:blocked` (HTTP 402) event bus.
- **Advisory-lock guard** on the candle aggregation loop (`pg_try_advisory_lock`) so it's safe to scale to multiple workers.

### 2.3 Critical issues

#### 🔴 C1 — The paywall does not exist (entitlements bypass)
`app/auth/entitlements.py`:
```python
# Treat all authenticated users as having full premium access
premium = bool(user_id)
tier: Tier = "elite" if user_id else "basic"
...
features: Set[Feature] = set(FEATURE_MATRIX.keys()) if premium else set()

def require_feature(feature: Feature):
    async def _dep(req: Request):
        return True   # <-- always allows
    return _dep
```
Every logged-in user is Elite; every gated route (`smart_buy_router` is mounted with `Depends(require_feature("smart_buy"))`) is open. Stripe can charge people, but nothing changes when they pay. **This is the single highest-ROI fix in the entire codebase** — it's both a revenue leak and a data-integrity problem (leaderboard, limits, etc. all assume tiers that aren't enforced). Wire `is_premium`/`tier` to the real `subscriptions`/`user_profiles.is_premium` state and restore `require_feature` to raise 402/403.

#### 🔴 C2 — Stack traces leaked to clients
Both the global `Exception` handler and `CatchExceptionsMiddleware` return `traceback.format_exc()` in the JSON body:
```python
error_msg = f"{str(exc)}\n{traceback.format_exc()}"
return JSONResponse(status_code=500, content={"detail": error_msg})
```
This exposes file paths, SQL, library versions, and internal structure to any attacker. Log the traceback server-side; return a generic message + error id to the client. There are **two** competing global exception handlers registered (lines ~917 and ~3852) — the second silently wins, which is itself a smell.

#### 🔴 C3 — Debug endpoint dumps session + cookies
```python
@app.get("/api/debug/session-state")
async def debug_session_state(request: Request):
    return {"session_data": dict(request.session), "cookies": dict(request.cookies), ...}
```
Unauthenticated, returns the caller's full session/cookie state. Combined with `__whichdb`, `__has_candles`, `/healthz` probes left in prod, this is reconnaissance surface. Remove or gate behind an admin check.

#### 🔴 C4 — OAuth guild-membership check disabled + weak state handling
```python
print(f"✅ Discord OAuth successful for user {discord_id}, skipping guild membership check")
```
`OAUTH_STATE` is an in-memory dict (lost on restart, not shared across workers), so on a multi-instance deploy the `state` CSRF check will spuriously fail or be bypassed. `check_server_membership()` also *returns `True` on any exception* ("fail open"). For an auth boundary, fail closed.

#### 🔴 C5 — Schema managed by runtime `CREATE TABLE IF NOT EXISTS` + `ALTER ... ADD COLUMN`
`lifespan()` runs ~30 DDL statements on every boot. This is slow, races across instances, silently swallows migration errors (`except Exception: logging.warning(...)`), and means your schema's source of truth is scattered between `lifespan`, `setup_database.sql`, and `migrations/*.sql` (which have **two different `001_`, two `006_`** files — ordering is ambiguous). Consolidate on a real migration tool (Alembic) run as a deploy step.

#### 🟠 C6 — `main.py` is 3,894 lines (and there's a stray 2,802-line `main` duplicate)
There is a file literally named `main` (no extension) that is an older divergent copy of `main.py` sitting in the repo root. That's a foot-gun waiting to be imported or deployed. Delete it. Then decompose `main.py` — auth, billing, trades, watchlist alerts, and market/momentum should each be routers/services. The size actively hides bugs (two exception handlers, duplicated `get_db`, duplicated `user_has_premium_role` defined in both `main.py` and `entitlements.py`).

#### 🟠 C7 — No tests, no monitoring, no structured logging
`vitest` and `@testing-library` are in `package.json` but there are no meaningful tests in any repo (`test_endpoints.sh` is a shell smoke script). Logging is `logging.basicConfig(INFO)` with emoji print statements. There is no error tracking (Sentry), no metrics, no scraper-health alerting. For a product whose value *is* fresh data, silent scraper failure = silent product death.

#### 🟠 C8 — Data-pipeline single points of failure
- `ea_price_sync.py` dies permanently when `EA_X_UT_SID` expires (401 → `break`) and requires a human to log into the FUT web app and paste a fresh token. No alert fires; prices just go stale.
- All scraping hits futbin from (presumably) one Railway egress IP with a static `User-Agent: SBCSolver/1.5`. One markup change or one Cloudflare tightening and every price endpoint returns `None`. There's no second source and no circuit breaker.
- `bin_sales_history_sync` runs every 10 min over ~2,500 gold cards × up to 4 requests. At 429 you back off, but there's no global budget — a bad run can stack.

#### 🟡 C9 — Correctness/quality smells
- **EA tax is hardcoded 5%** in many places (`ea_tax = int(round(sell * qty * 0.05))`). Fine today, but it's duplicated ~6 times; centralize it.
- **`fetch_price()`** reads `fut_players.price_num` (daily-ish) but the watchlist "price alerts" imply live prices — alerts will be stale by up to a day for cards the 10-min sales sync doesn't cover.
- **`get_player_price` imported but "not strictly required"** per its own comment — dead-ish imports linger.
- **Platform lie:** Xbox is silently mapped to PS everywhere (`fb_plat = "pc" if platform == "pc" else "ps"`). Defensible (shared console market in FC) but the UI offers "xbox" as a distinct choice, so users think they're getting Xbox data they aren't. Document it in the UI.
- **`ai_engine.copilot`** is a giant if/elif keyword matcher branded as "AI Copilot." It's honest pattern-matching with *one* genuinely data-grounded branch (price lookup). Fine as a v0, but don't market it as AI until it is.
- **Blocking-ish work in request path:** `top_buys` fans out live futbin history fetches (bounded to 8) inside a user request — p95 latency will be brutal under load. Precompute this.
- **`datetime.utcnow()`** (naive) in leaderboard vs `datetime.now(timezone.utc)` elsewhere — mixed tz-awareness is a latent bug source.

#### 🟡 C10 — Frontend
- **28 `console.log` calls** shipped (including axios env dump). Strip in prod build.
- **Two axios instances** (`src/axios.js` — the good one — and `src/utils/axios.js` — a naive duplicate). Consolidate.
- **`main.py` equivalent on the client:** `Dashboard.jsx` (1,029 lines), `Settings.jsx` (929), `SmartBuy.jsx` (908) are monoliths mixing data-fetch, layout, and business logic.
- **No global data cache** (React Query / SWR). Every page refetches; the axios retry layer helps but you're re-hitting the API for data that rarely changes (player metadata, prices within TTL).
- **`user-scalable=no`** in the viewport meta hurts accessibility (blocks pinch-zoom).
- **PWA is half-wired** — `registerSW` exists and `sw.js`/manifest are present, but there's no evidence the SW is registered from `main.jsx`, and icons are "optional / place actual files." Either commit to installable PWA or drop the dead weight.

### 2.4 Data-model assessment

`sales_history` and `bin_history` are the strong core. Gaps:
- **No `players`↔`sales` FK / no partitioning.** `sales_history` will grow fast (2,500 cards × sales × forever, "nothing is ever deleted"). Add time-based partitioning + a retention/rollup policy (keep raw 90 days, roll up to hourly/daily candles beyond).
- **`fut_candles` uses a different id space** (`player_card_id`) than `sales_history.player_id` — the comment in `ai_engine.py` admits this. Two incompatible id schemes for the same players is tech debt that will bite every analytics feature.
- **`trades` has no FK to `users`**, `user_id` is free-text, and `trade_id` uniqueness is patched via a backfill query on boot. Money/portfolio data deserves stronger integrity.
- **No index review** — several hot queries (`sales_history WHERE player_id AND sold_at >= ...`) need a composite `(player_id, sold_at DESC)` index to stay fast as the table grows.

---

## 3. Current Feature Analysis

### What's strong
| Feature | Verdict |
|---|---|
| Trade journal + dashboard (profit, tax, win-rate, tags) | Solid, genuinely useful, the retention hook. |
| Watchlist + Discord price/liquidity alerts | Great idea, differentiated (Discord-native), but historically buggy (alert loop). |
| Real sales/BIN history + Player Search charts | **Best-in-class data**, underexposed in UI. |
| Public historical-data API (keys, rate limit) | **Unique.** Nobody else productizes this. |
| Squad builder w/ chemistry | Table-stakes but present and complete. |
| Trending / momentum | Good, but derived from futbin momentum scrape (fragile). |

### Weak or missing
- **Data is under-visualized.** You compute median-sold, divergence, sales/hour, stddev in `public_market_metrics` — but the app barely surfaces them. This is the "we have the data but don't use it" problem in one endpoint.
- **No predictive layer.** Everything is descriptive (what *was* the price). No forecast, no "fair value," no "this is X% below where it usually sits."
- **No real-time.** Everything is poll + cache. No WebSocket/SSE for live prices or alerts in-app (alerts only go to Discord).
- **Copilot isn't AI**, Smart Buy scoring is heuristic and computed in-request.
- **Social/community is half-migrated** — migrations for a full social trading feed (traders, subscriptions, posts, tips) exist but the live feature set doesn't clearly expose them.
- **Mobile:** there's a `MobileNavigation` and responsive Tailwind, but no native app and PWA is incomplete.
- **Onboarding/empty states**: a new user with zero trades gets a lot of "N/A."

---

## 4. Data Utilization Opportunities

You have the raw material for things competitors structurally cannot do. Ranked by value/effort:

### 4.1 "Fair Value" & mispricing signal (do this first — it's a query)
You already compute median-sold-24h vs current BIN divergence in `public_market_metrics`. Promote it to a first-class, precomputed signal for every tracked card:

```sql
-- materialized view, refreshed every few minutes
SELECT
  s.player_id,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY s.sold_price)
     FILTER (WHERE s.sold_at >= now() - interval '24 hours')                 AS fair_value_24h,
  percentile_cont(0.5) WITHIN GROUP (ORDER BY s.sold_price)
     FILTER (WHERE s.sold_at >= now() - interval '7 days')                   AS fair_value_7d,
  count(*) FILTER (WHERE s.sold_at >= now() - interval '24 hours')           AS liquidity_24h,
  stddev_pop(s.sold_price) FILTER (WHERE s.sold_at >= now() - interval '24 hours') AS volatility_24h,
  b.lowest_bin                                                               AS current_bin
FROM sales_history s
JOIN LATERAL (
  SELECT lowest_bin FROM bin_history
  WHERE player_id = s.player_id ORDER BY captured_at DESC LIMIT 1
) b ON true
GROUP BY s.player_id, b.lowest_bin;
```
Then `discount_pct = (fair_value_24h - current_bin) / fair_value_24h`. Rank the whole market by it. That's an "undervalued right now" board grounded in *completed sales*, not vibes. **This alone is a headline feature.**

### 4.2 Anomaly / opportunity detection
Flag cards where `current_bin` drops below `fair_value_24h − k·volatility_24h` on rising liquidity → a real "sniping opportunity" alert. Z-score on the sold-price series; alert on BIN crossing −2σ. Push to Discord + in-app.

### 4.3 Price prediction (the moonshot that's actually reachable)
Your sales timeline supports supervised learning: features = recent slope, day-of-week, hours-to-known-event (`events` table already exists!), rating, league popularity, promo flag. Target = price at t+6h / t+24h. Start with gradient-boosted trees (LightGBM) offline, serve predictions as a nightly batch into a `price_forecast` table. You don't need real-time inference to launch — a daily "predicted direction + confidence" per card is already more than any competitor offers.

### 4.4 Event correlation
`events` (promos, "next promo" widget) × price series = "cards like this historically rose X% the 48h after a TOTW drop." This is *the* content FUT traders want and it's a `JOIN` over data you already store.

### 4.5 User-behavior analytics for retention
`trades` + `smart_buy_feedback` → cohort retention, which features precede a subscription, which alert types drive log-ins. Segment by console, tier, and inferred playstyle (flipper vs investor from hold-time). Feed it back into personalized recommendations.

### 4.6 Segmented market insights
Aggregate your own data into content: "Most-traded 84-rated cards this week," "Biggest fallers post-promo," "Meta index." This doubles as SEO/marketing (see §6).

---

## 5. Innovative New Features

### Must-Have (fills obvious gaps; mostly re-uses existing data)
1. **Fair-Value badge everywhere** — every price shown gets a green/red "X% under/over real sold value" chip (from §4.1). Immediate, visible, unique.
2. **In-app real-time alerts** via SSE (you already have the alert engine; just also push to the browser, not only Discord).
3. **Profit calculator + tax reporting that's actually rich** — you have trades; add monthly P&L, best/worst cards, hold-time analysis, CSV/PDF export. Traders love spreadsheets.
4. **Precomputed Smart Buy board** — move `top_buys` out of the request path into a materialized table refreshed every N minutes; add the fair-value signal.
5. **Proper onboarding** — demo portfolio, sample alerts, guided first trade log.

### Differentiators (things FUTBIN/FUTWIZ don't do well)
6. **The Historical Data API as a product** (see §6) — sell structured sales data to creators/bot-builders. You've already built it; package and price it.
7. **SBC Solver with live market cost optimization** — solve squad-building challenges minimizing *current* coin cost using your own live BINs, with "cheapest solution right now" + "wait, this fodder is 20% above fair value." This is a top-3 requested FUT tool and your live pricing makes it credible.
8. **Evolution / SBC profitability planner** — "invest in these untradeable-adjacent cards before this SBC drops."
9. **Copy-trader / social feed** — you have the migrations; finish it. Let verified traders post signals, users follow, and (see §6) charge for premium signals with a revenue split.
10. **Portfolio tracking with live mark-to-market** — user logs holdings, dashboard shows unrealized P&L against live BINs and fair value. Turns the journal into a reason to open the app daily.

### Wow Factors (moonshots, defensible)
11. **Price-prediction engine** (§4.3) with backtested accuracy shown transparently ("our 24h direction call was right 63% of the time last month" — honesty builds trust).
12. **"Market Radar" live anomaly feed** — a streaming board of statistically unusual price moves as they happen. Bloomberg-terminal energy for FUT.
13. **Real-world football signal integration** — injuries/form/fixtures (via a football-data API) correlated to card demand ("Haaland scored a hat-trick → SBC/CL cards trending"). Genuinely novel.
14. **AI trade-journal analyst** (real LLM this time) that reads a user's trade history and writes a weekly coaching report grounded in their actual numbers + your market data.
15. **Sentiment from X/Reddit/YouTube leak channels** — NLP on FUT content to front-run hype cycles. Hard, but nobody has it.

---

## 6. Monetization Plan

Right now: Stripe is wired, the paywall isn't enforced (C1). Fixing C1 *is* monetization step zero.

### Freemium structure (recommend collapsing to 3 clear tiers)
| | **Free** | **Pro (~£6–8/mo)** | **Elite (~£15–20/mo)** |
|---|---|---|---|
| Trade journal & dashboard | ✅ | ✅ | ✅ |
| Player search + basic price | ✅ | ✅ | ✅ |
| Watchlist size | 3 | 25 | 500 |
| Fair-value signal | teaser (blurred %) | ✅ | ✅ |
| Real-time in-app alerts | ❌ | ✅ | ✅ priority |
| Trending timeframes | 24h | 6h/24h | 4h/6h/24h |
| Smart Buy / Trade Finder | ❌ | ✅ | ✅ |
| Price predictions | ❌ | direction only | direction + confidence + targets |
| SBC solver (market-optimized) | limited | ✅ | ✅ |
| Portfolio mark-to-market | ❌ | ✅ | ✅ |
| Ad-free | ❌ | ✅ | ✅ |
| Public Data API quota | ❌ | small | large |

Your `FEATURE_MATRIX` already models this — you just aren't enforcing it. The infra is 80% built.

### Revenue streams (multiple, none scammy)
1. **Subscriptions** — primary. With a fixed paywall and a genuine predictive feature, a 3–5% free→paid conversion on an engaged FUT audience is realistic.
2. **Historical Data API** — tiered API keys (you built the metering). Sell to content creators, Discord-bot makers, spreadsheet traders. £10–50/mo tiers by quota. **This is your most defensible B2B line** because the data is proprietary.
3. **Creator / copy-trading marketplace** — verified traders sell premium signals; you take 20–30% (migrations already model `trader_subscriptions`, `content_purchases`, tips). Aligns with the community and monetizes UGC.
4. **Data-insight reports / licensing** — anonymized aggregate market reports ("FC26 Q3 market index") licensed to creators/media.
5. **Non-intrusive ads for free tier only** — a single tasteful unit; killed instantly by any paid tier (which is itself a conversion lever).
6. **One-off credits** — pay-per-use for expensive tools (full-market SBC solve, deep backtest) for users who won't subscribe.

**Avoid:** coin-seller affiliates. It's against EA ToS, it poisons trust, and it puts your Discord/EA relationship at risk. The whole brand value is "legit tools" — don't trade that for affiliate pennies. Gaming-peripheral/energy-drink affiliates are fine if you must.

### Conversion mechanics
- Show the *value* free, gate the *action*: display "this card is 18% under fair value" but blur the exact number and the buy-target until Pro.
- Alert quota that bites right when a user gets hooked (3 watchlist slots → "add a 4th" upsell).
- Weekly "here's what you'd have made with Pro predictions" email.

---

## 7. Technical Recommendations & Integrations

### Data sources & scraping resilience
- **Harden the EA sync:** detect `EA_X_UT_SID` expiry and *alert* (Discord/email) instead of silently stopping; support multiple rotating session tokens; consider the official EA companion flow limits and back off hard to avoid account risk. Treat EA scraping as the highest-ban-risk surface — rate-limit conservatively, randomize timing, never parallelize aggressively.
- **Add a second price source** so futbin isn't a SPOF. Even a cross-check between EA BIN and futbin catches markup breakage early.
- **Proxy/egress:** move scrapers behind a small rotating residential/datacenter proxy pool with a shared politeness budget; monitor 403/429 rates as a first-class metric.
- **Legal/ToS reality:** futbin scraping is a gray area (respect robots, keep rate low, cache aggressively — you already do). EA API scraping carries *account* risk. Prefer official/semi-official feeds where they exist; document that risk internally. Your own `sales_history` reduces dependence over time — the more history you bank, the less each live fetch matters.
- **External football data** (API-Football / Sportmonks / understat): injuries, fixtures, form → demand signals (§5.13). Legitimate, licensable APIs exist.

### Real-time & scale
- **Redis** for the caches currently living in per-process dicts (`_MOMENTUM_CACHE`, `_price_cache`, `_RATE_WINDOW`, `_ROLE_CACHE`). Those don't survive restarts or scale across instances — the API-key rate limiter is *per-process*, so it's already wrong the moment you run two workers.
- **SSE** (simplest) or WebSockets for live in-app prices/alerts.
- **Precompute, don't compute-in-request:** materialized views + a refresher worker for Smart Buy, Trending, Fair Value. Serve reads from tables.
- **Promo-spike scaling:** content drops = traffic spikes. Put the read API behind a CDN/edge cache for public endpoints, autoscale the web tier, and make sure the DB read replica serves the player DB.
- **Partition `sales_history` by month; add rollup candles;** index `(player_id, sold_at DESC)`.

### Engineering hygiene
- Alembic migrations run once on deploy; delete runtime DDL and the stray `main` file.
- Sentry + structured JSON logging + basic Prometheus/Grafana (or Railway metrics) with a **scraper-freshness alert** ("no new sales rows in 30 min").
- Split `main.py`; add a real test suite (pytest for the SQL-heavy services, Vitest for React). Start with the money paths: entitlements, Stripe webhooks, tax/profit math.
- CI: lint + typecheck + tests on PR. Consider migrating the frontend fully to TypeScript (you already have `.tsx` files leaking in).

---

## 8. Prioritized Roadmap

### Quick wins (0–4 weeks) — "stop the bleeding, unlock revenue"
1. **Fix entitlements & `require_feature` (C1)** → the paywall goes live. *Highest ROI.*
2. Remove stack-trace leakage (C2), debug endpoints (C3), stray `main` file (C6).
3. Fail-closed OAuth + move `OAUTH_STATE` to Redis (C4).
4. EA-session-expiry alerting + scraper freshness monitoring (C8).
5. Ship **Fair-Value badge + "Undervalued now" board** off existing data (§4.1) — first visible differentiator.
6. Strip client `console.log`s, consolidate the two axios clients.

### Medium term (1–3 months) — "differentiate & harden"
7. Alembic migrations; delete runtime DDL; unify the `player_id`/`player_card_id` id space.
8. Redis-backed caches + rate limiting; precompute Smart Buy/Trending into tables.
9. In-app real-time alerts (SSE); anomaly-detection alert type (§4.2).
10. Finish the social/copy-trading feature (migrations already exist) + creator revenue split.
11. Market-optimized **SBC solver** (§5.7) — a genuine acquisition magnet.
12. Package & launch the **Data API** publicly with pricing (§6.2).
13. Test suite on money paths; Sentry; split `main.py`.

### Long term (3–9 months) — "moat"
14. **Price-prediction engine** (§4.3) with transparent backtested accuracy → Elite headline.
15. **Market Radar** live anomaly stream (§5.12).
16. Real-world football-data correlation (§5.13) and content/SEO engine off aggregate stats.
17. Native/complete-PWA mobile app.
18. Second data source + proxy infra so the pipeline is genuinely resilient at scale.

---

## 9. Other Insights

- **Differentiation in one line:** *"The only FUT platform built on real completed-sales data — with a fair-value signal, a market-optimized SBC solver, and a price-prediction engine nobody else can match."* Descriptive tools are commoditized; **predictive + proprietary-data** tools are not.
- **Retention loop:** journal (why they log trades) → alerts + fair-value (why they come back daily) → predictions + portfolio (why they pay) → leaderboard/social (why they bring friends). Each layer feeds the next; you have pieces of all four already.
- **Growth hacks:** the Data API + aggregate stats double as SEO/marketing (creators cite you); referral system already exists (finish gating it correctly); Discord-native alerts are a viral surface — make shared alerts include a branded invite.
- **Risks to manage deliberately:**
  - *EA account/IP bans* on the scrapers — your biggest existential risk. Diversify sources, bank history, rate-limit hard, never touch coin-selling.
  - *Cloudflare/markup changes* — monitoring + a second source.
  - *Legal* — keep scraping polite/cached, don't redistribute futbin's branding, lean on your *own* accumulated data as the product.
  - *Competition* — FUTBIN has scale; you win on data depth + prediction + UX, not on breadth. Don't try to out-FUTBIN FUTBIN.
- **Do the boring fix first.** None of the exciting features matter while every paying customer gets the product for free and one expired cookie can dark the whole site. Fix C1 and C8 this week; everything else compounds from there.

---

*Prepared as a full-repo review across `backend`, `auto_sync`, and `frontend_new`. Line references were accurate at time of review; the codebase is under active development.*
