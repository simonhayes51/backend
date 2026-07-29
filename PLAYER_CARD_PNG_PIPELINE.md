# Player Card PNG Generation Pipeline

Flattens the live `PlayerCardArt` React component (background + cutout +
rating/stats/badges/skill-moves/etc.) into one transparent PNG per card,
uploads it to S3-compatible object storage, and links it back to the
`fut_players` row for reuse (search thumbnails, downloads, social sharing,
embeds) instead of re-rendering the live HTML card every time.

## Architecture

The frontend (`frontend_new`) is a Vite SPA with no server runtime of its
own, so there's no way to render the real React component server-side
inside that repo. Instead:

1. **backend** launches headless Chromium (Playwright) and navigates it to
   a route inside the **already-deployed frontend**:
   `{FRONTEND_URL}/#/internal/render/player-card/{card_id}?token=...`
2. That route (`frontend_new/src/pages/internal/PlayerCardExport.jsx`)
   fetches render data from a token-gated backend endpoint, then renders
   `<PlayerCardArt exportMode ... />` - the *same* component every other
   page uses, just with `exportMode` turned on (fixed 432×576 canvas,
   transparent background, every field drawn, no interactive/hover/page
   chrome).
3. Once fonts + every image inside the card have settled, the page sets
   `document.documentElement.dataset.cardReady = "true"`.
4. Chromium (still on the backend) waits for that marker, then screenshots
   only the `[data-player-card-export]` element with `omit_background=True`
   - i.e. a real image of the actual rendered component, not a
   reimplementation of the card in Python/Pillow.
5. The PNG is uploaded to S3-compatible storage and the resulting URL,
   storage key, and a content hash are written back to `fut_players`.

```
Admin click / bulk script
        │
        ▼
ensure_generated_player_card(card_id)      [backend/app/services/player_card_generation.py]
        │  1. fetch player row + compute render hash
        │  2. hash unchanged + status=ready? → return cached URL, done
        ▼
render_player_card_png(card_id)            [backend/app/services/player_card_render.py]
        │  headless Chromium → frontend's /internal/render/player-card/:id
        ▼
upload_png(key, bytes)                     [backend/app/services/object_storage.py]
        │  S3-compatible PUT, immutable cache-control
        ▼
UPDATE fut_players SET generated_card_* ...
```

### Why this hash, not a Next.js/Satori/html2canvas approach

- The frontend has no server runtime (confirmed: Vite + `serve`, no
  App/Pages Router) - a screenshot of the deployed SPA is the only way to
  reuse the real component without reimplementing the card visually a
  second time.
- Chromium screenshots aren't affected by the CORS-tainted-canvas problem
  that sinks `html2canvas`/`dom-to-image` with hotlinked FUTBIN/fut.gg
  image URLs - a real screenshot doesn't go through `canvas.toDataURL()`.

## Files

**backend**
- `migrations/025_player_card_png.sql` - `generated_card_*` columns + status index on `fut_players`
- `app/services/player_card_data.py` - single source of truth for "everything drawn on the card" (shared by hashing and the render-data endpoint)
- `app/services/player_card_hash.py` - deterministic SHA-256 render hash + `PLAYER_CARD_RENDER_VERSION`
- `app/services/player_card_token.py` - short-lived signed tokens gating the internal render route
- `app/services/player_card_render.py` - Playwright screenshot capture
- `app/services/object_storage.py` - S3-compatible upload client
- `app/services/player_card_generation.py` - `ensure_generated_player_card()`, the cache/regenerate/status-lifecycle orchestrator
- `app/routers/player_cards.py` - `/api/internal/render/player-card/{id}` (token-gated) + `/api/admin/player-cards/{id}/generate|status` (admin-gated)
- `scripts/generate_player_cards.py` - bulk backfill script
- `tests/test_player_card_pipeline.py` - hash/token/storage-key unit tests

**frontend_new**
- `src/components/PlayerCardArt.jsx` - added `exportMode` (+ `skillMoves`/`weakFoot`/`preferredFoot`/`altPositions`/`exportWidth`/`exportHeight` props)
- `src/pages/internal/PlayerCardExport.jsx` - the render route Chromium screenshots
- `src/v2/pages/Admin/tabs/PlayerCardsTab.jsx` - admin generate/regenerate/status control
- `src/v2/pages/PlayerPage/sections/HeaderSection.jsx`, `src/components/PlayerSearch.jsx` - "Download card PNG" button when `generated_card_url` exists

## Environment variables (backend)

| Variable | Required | Notes |
|---|---|---|
| `AWS_REGION` | no | defaults to `auto` (fine for R2) |
| `AWS_ACCESS_KEY_ID` | yes | |
| `AWS_SECRET_ACCESS_KEY` | yes | |
| `AWS_S3_BUCKET` | yes | |
| `AWS_PUBLIC_BASE_URL` | yes | public base URL the uploaded key is appended to, no trailing slash |
| `S3_ENDPOINT` | no | set for Cloudflare R2 / any non-AWS S3-compatible provider |
| `S3_FORCE_PATH_STYLE` | no | `1`/`true` if your provider needs path-style addressing |
| `PLAYER_CARD_RENDER_SECRET` | no | falls back to `SECRET_KEY`; set to rotate independently |
| `PLAYER_CARD_RENDER_TIMEOUT_MS` | no | default `20000` |
| `PLAYER_CARD_READY_TIMEOUT_MS` | no | default `15000` |
| `FRONTEND_URL` | already required | reused - this is what Chromium navigates to |

## Storage path convention

```
fc26/generated-player-cards/{card_id}/{hash[:16]}.png
```

Immutable per (card_id, hash) pair - a regeneration with a changed hash
gets a new key, it never overwrites the previous object in place. Old
objects are not garbage-collected automatically (out of scope here); if
you want that, sweep keys whose hash prefix no longer matches the current
`generated_card_hash`.

## Render hash & caching

`compute_card_render_hash()` hashes every field that visibly affects the
card (rating, position, stats, skill moves, weak foot, foot, alt
positions, card art URLs, nation/club/league, version, name) plus
`PLAYER_CARD_RENDER_VERSION`. It deliberately excludes price/games-played/
chemistry-style fields - those change constantly and never affect the
image, so hashing them would defeat caching entirely.

**Bump `PLAYER_CARD_RENDER_VERSION`** in `app/services/player_card_hash.py`
whenever the export template's *layout* changes (new field drawn, resized
canvas, repositioned badge) - every existing card is treated as stale on
its next `ensure_generated_player_card()` call, even though none of its
underlying data changed.

`ensure_generated_player_card()` skips regeneration when
`generated_card_status = 'ready'` and the stored hash matches the freshly
computed one; `force=True` always regenerates.

## Generate one card (admin)

Admin UI: `/v2/admin` → "Player Cards" tab → enter a card ID → Generate /
Force regenerate.

Or directly:
```bash
curl -X POST https://<backend>/api/admin/player-cards/12345/generate \
  -H "Content-Type: application/json" \
  --cookie "<admin session cookie>" \
  -d '{"force": false}'
```

## Bulk generate missing/stale cards

Not an `npm run` script - this backend is Python/FastAPI, not Node, so
there's no `package.json` to hang an npm script off. Run directly:

```bash
# Cards with no PNG yet, or whose last attempt errored
python -m scripts.generate_player_cards --missing --limit=100 --concurrency=1

# Cards whose stored hash no longer matches current data
python -m scripts.generate_player_cards --stale --limit=200

# One specific card, bypassing the cache
python -m scripts.generate_player_cards --player-id=12345 --force
```

Concurrency defaults to 1 (Chromium is heavy relative to this app's usual
aiohttp/asyncpg workloads) and one Chromium process is reused across the
whole batch (a fresh context/page per card, not a fresh browser). This is
meant to be run by hand or as a one-off Railway job - it is **not** wired
up as a permanent Railway worker/cron.

## Railway / Playwright

`nixpacks.toml` adds a `[phases.playwright]` phase (`playwright install
--with-deps chromium`) depending on the existing `install` phase, mirroring
the pattern already used by the sibling `auto_sync` repo. `requirements.txt`
gained `boto3`, `botocore`, and `playwright`.

Unlike `auto_sync`'s FUTBIN scraper (which needs headed Chromium + Xvfb
because Cloudflare blocks headless requests from Railway's IP range), this
renders our **own** frontend, so plain headless Chromium works fine - no
Xvfb, no anti-bot handling needed.

## Troubleshooting

- **Card generation times out around 15-20s**: the render route never set
  `data-card-ready`. Usually a font/image that never resolved - check
  `PlayerCardExport.jsx`'s image-settle logic and confirm `FRONTEND_URL`
  actually points at a reachable, deployed frontend (not `localhost`, from
  Railway's network).
- **Generation errors with "bg-image-failed"**: the card's background
  image URL 404'd/CORS-blocked even through the `/img?url=` proxy. Check
  `card_bg_image`/`image_url` for that `card_id`.
- **"Missing required env var: AWS_S3_BUCKET"** (or similar): one of the
  storage env vars above isn't set in this environment.
- **Playwright launch fails on Railway ("Executable doesn't exist")**: the
  `[phases.playwright]` nixpacks phase didn't run - check the build log for
  `playwright install --with-deps chromium`.

## Manual test steps

1. Set all storage env vars + `FRONTEND_URL` pointing at a real deployed
   (or tunnelled) frontend.
2. Run migration 025 (automatic on boot, or `python scripts/run_migrations.py --player`).
3. Pick a real `card_id` from `fut_players` and call the admin generate
   endpoint (or the admin UI tab).
4. Confirm: response has `generated: true`, a `hash`, `864x1152` (or your
   configured export size), and an `imageUrl`.
5. Open `imageUrl` directly - confirm it's a real transparent PNG (no
   white/black page background), the card layout matches the live
   component, and skill moves/weak foot/foot/alt positions are visible.
6. Call generate again with the same card, `force` omitted - confirm
   `generated: false` (cache hit, same URL).
7. Call generate again with `force: true` - confirm `generated: true` and
   a **different** storage key (hash-versioned, old object still exists).
8. Edit that card's rating (or any hashed field) in the DB directly, call
   generate without `force` - confirm it regenerates automatically because
   the hash no longer matches.
9. Temporarily point `card_bg_image` at an invalid URL for a test card,
   regenerate, confirm `generated_card_status = 'error'` with a readable
   `generated_card_error`, and that any *previous* valid
   `generated_card_url` was not wiped out.
