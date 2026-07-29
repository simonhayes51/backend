# app/services/player_card_token.py
#
# Short-lived, HMAC-signed tokens that gate the internal render-data
# endpoint (GET /api/internal/render/player-card/{card_id}). That route
# can't be protected by the normal session-cookie require_admin dependency
# because the caller is headless Chromium, driven by our own backend, with
# no user session at all - so instead we mint a token server-side (in
# player_card_generation.py, right before launching Chromium) and the
# render page simply forwards it back as a query param. Nothing else can
# forge a valid token without SECRET_KEY / PLAYER_CARD_RENDER_SECRET, and
# a valid token expires in minutes, so it's not a usable public API even if
# the URL leaks into a browser history or proxy log.
from __future__ import annotations

import os

from itsdangerous import URLSafeTimedSerializer

_SALT = "player-card-render-v1"

# Falls back to SECRET_KEY (already required for session cookies - see
# RAILWAY_SETUP.md) rather than forcing yet another mandatory env var on
# deploy; set PLAYER_CARD_RENDER_SECRET explicitly to rotate this
# independently of the session-cookie secret.
_SECRET = os.getenv("PLAYER_CARD_RENDER_SECRET") or os.getenv("SECRET_KEY") or "dev-insecure-player-card-render-secret"


def _serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(_SECRET, salt=_SALT)


def make_render_token(card_id: str) -> str:
    return _serializer().dumps(str(card_id))


def verify_render_token(card_id: str, token: str, max_age_seconds: int = 120) -> bool:
    if not token:
        return False
    try:
        signed_card_id = _serializer().loads(token, max_age=max_age_seconds)
    except Exception:
        return False
    return signed_card_id == str(card_id)
