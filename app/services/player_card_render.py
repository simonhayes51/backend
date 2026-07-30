# app/services/player_card_render.py
from __future__ import annotations

import logging
import os
import struct
from dataclasses import dataclass
from typing import Optional

from playwright.async_api import Browser, Page, async_playwright

from app.services.player_card_token import make_render_token

logger = logging.getLogger("player_card_render")

# Exact measured FUTBIN large-card wrapper: 252px wide, 350px artwork,
# 354.8px total wrapper height. DPR 2 produces a crisp 504x710 PNG.
EXPORT_WIDTH = 252
EXPORT_HEIGHT = 355
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
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise PlayerCardRenderError("Screenshot buffer is not a valid PNG")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


async def _capture_page(page: Page, card_id: str) -> RenderedCard:
    """Render one card using an existing page.

    Bulk jobs reuse one page per worker. That avoids creating a fresh browser
    context and page for every card and lets Chromium reuse fonts, JS and image
    cache across the whole run.
    """
    token = make_render_token(str(card_id))
    url = f"{_FRONTEND_URL}/#/internal/render/player-card/{card_id}?token={token}"

    page.set_default_timeout(_NAV_TIMEOUT_MS)
    # cardReady is the real readiness contract. Waiting for networkidle as well
    # made every card wait on unrelated long-lived/browser requests.
    await page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)

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

    # cardReady can become true via a timeout fallback even when one or more
    # background/cutout card-frame image layers never actually finished
    # loading - that's exactly the "face-only crop" bug seen in production.
    # The frontend sets data-card-degraded='true' in that case; treat it as
    # a hard failure rather than letting a partial render report success, so
    # it goes through ensure_generated_player_card's normal error/retry path
    # instead of getting cached forever as a good card.
    is_degraded = await page.evaluate(
        "document.documentElement.dataset.cardDegraded === 'true'"
    )
    if is_degraded:
        raise PlayerCardRenderError(
            f"Card {card_id}: degraded render - background/cutout image layer(s) "
            "failed to load before the ready-timeout fallback fired"
        )

    png_bytes = await card.screenshot(type="png", omit_background=True)
    if not png_bytes:
        raise PlayerCardRenderError(f"Card {card_id} screenshot returned an empty buffer")

    width, height = _png_dimensions(png_bytes)
    return RenderedCard(png_bytes=png_bytes, width=width, height=height)


async def _capture(browser: Browser, card_id: str) -> RenderedCard:
    context = await browser.new_context(
        viewport={"width": EXPORT_WIDTH, "height": EXPORT_HEIGHT},
        device_scale_factor=EXPORT_DEVICE_SCALE,
    )
    try:
        page = await context.new_page()
        return await _capture_page(page, card_id)
    finally:
        await context.close()


async def render_player_card_png(
    card_id: str,
    browser: Optional[Browser] = None,
    page: Optional[Page] = None,
) -> RenderedCard:
    if page is not None:
        return await _capture_page(page, card_id)
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
