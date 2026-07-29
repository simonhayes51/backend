# app/services/player_card_render.py
#
# Server-side "flatten the live PlayerCardArt component into one
# transparent PNG" renderer. There's no headless-browser usage anywhere
# else in this repo (confirmed: no playwright/puppeteer/selenium) and the
# frontend is a Vite SPA with no server runtime of its own (no Next.js
# API routes/RSC to host a renderer inside), so the only way to reuse the
# *actual* React component - rather than reimplementing the card visually
# a second time in Python/Pillow - is to point real Chromium at the
# deployed frontend's own internal render route and screenshot just that
# one element.
#
# Unlike auto_sync's futbin_sbc_sync.py (which needs headed Chromium +
# Xvfb because FUTBIN's Cloudflare blocks headless requests from Railway's
# IP range), this navigates to our *own* frontend, so plain headless
# Chromium is fine - no Xvfb, no anti-bot fingerprinting concerns.
from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Browser, async_playwright

from app.services.player_card_token import make_render_token

logger = logging.getLogger("player_card_render")

# CSS pixel canvas. Chosen to match PlayerCardArt's own default aspect
# ratio (0.75, i.e. width/height - see cardAspect's useState default in
# PlayerCardArt.jsx) so exportMode doesn't distort the card versus its
# live on-screen proportions, close to a FUTBIN mobile standalone export's
# ~435x576 (0.755) without introducing an odd, non-round pixel size.
EXPORT_WIDTH = 432
EXPORT_HEIGHT = 576
# 2x device scale -> 864x1152 actual PNG output, crisp on high-DPI
# surfaces without the layout ever having to know about scaling.
EXPORT_DEVICE_SCALE = 2

_FRONTEND_URL = os.getenv("FRONTEND_URL", "https://app.futhub.co.uk").rstrip("/")
_NAV_TIMEOUT_MS = int(os.getenv("PLAYER_CARD_RENDER_TIMEOUT_MS", "20000"))
_READY_TIMEOUT_MS = int(os.getenv("PLAYER_CARD_READY_TIMEOUT_MS", "15000"))

_EXPORT_SELECTOR = "[data-player-card-export]"


class PlayerCardRenderError(RuntimeError):
    pass


@dataclass
class RenderedCard:
    png_bytes: bytes
    width: int
    height: int


def _png_dimensions(data: bytes) -> tuple[int, int]:
    """Reads width/height straight out of the IHDR chunk - no Pillow
    dependency exists anywhere in this repo, and we only need two ints,
    not general image decoding."""
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PlayerCardRenderError("Screenshot buffer is not a valid PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


async def _capture(browser: Browser, card_id: str) -> RenderedCard:
    token = make_render_token(str(card_id))
    url = f"{_FRONTEND_URL}/#/internal/render/player-card/{card_id}?token={token}"

    context = await browser.new_context(
        viewport={"width": EXPORT_WIDTH, "height": EXPORT_HEIGHT},
        device_scale_factor=EXPORT_DEVICE_SCALE,
    )
    try:
        page = await context.new_page()
        page.set_default_timeout(_NAV_TIMEOUT_MS)
        await page.goto(url, wait_until="networkidle", timeout=_NAV_TIMEOUT_MS)

        card = page.locator(_EXPORT_SELECTOR)
        await card.wait_for(state="attached", timeout=_NAV_TIMEOUT_MS)

        try:
            await page.wait_for_function(
                "document.documentElement.dataset.cardReady === 'true'",
                timeout=_READY_TIMEOUT_MS,
            )
        except Exception as exc:
            raise PlayerCardRenderError(
                f"Card {card_id} never signalled data-card-ready within {_READY_TIMEOUT_MS}ms "
                "(fonts/images/data likely failed to settle)"
            ) from exc

        error_marker = await card.get_attribute("data-card-export-error")
        if error_marker:
            raise PlayerCardRenderError(f"Card {card_id} export marked itself failed: {error_marker}")

        png_bytes = await card.screenshot(type="png", omit_background=True)
    finally:
        await context.close()

    if not png_bytes:
        raise PlayerCardRenderError(f"Card {card_id} screenshot returned an empty buffer")

    width, height = _png_dimensions(png_bytes)
    return RenderedCard(png_bytes=png_bytes, width=width, height=height)


async def render_player_card_png(card_id: str, browser: Optional[Browser] = None) -> RenderedCard:
    """Renders one card's export PNG. Pass an already-launched `browser` to
    reuse it across many cards (the bulk script does this - see
    scripts/generate_player_cards.py); omit it for a single ad-hoc
    generation (the admin API route does this), in which case a Chromium
    instance is launched and closed just for this one call."""
    if browser is not None:
        return await _capture(browser, card_id)

    async with async_playwright() as pw:
        launched = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        try:
            return await _capture(launched, card_id)
        finally:
            await launched.close()
