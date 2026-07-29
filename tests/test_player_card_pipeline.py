"""
Tests for the player-card PNG generation pipeline's pure-function pieces:
render-hash determinism (app/services/player_card_hash.py), render tokens
(app/services/player_card_token.py), and storage-key/upload-buffer
validation (app/services/object_storage.py). Deliberately excludes the
Playwright render/upload/DB round-trip itself - that needs a live browser,
network, and database and is exercised by manual generation instead (see
docs/PLAYER_CARD_PNG_PIPELINE.md's manual test steps).
"""
from __future__ import annotations

import pytest

from app.services.player_card_hash import PLAYER_CARD_RENDER_VERSION, compute_card_render_hash
from app.services.player_card_token import make_render_token, verify_render_token
from app.services.object_storage import ObjectStorageError


def _base_row(**overrides):
    row = {
        "card_id": 12345,
        "name": "Cristiano Ronaldo",
        "card_name": "Cristiano Ronaldo",
        "rating": 91,
        "version": "Rare Gold",
        "position": "ST",
        "altposition": "LW,RW",
        "pace": 87,
        "shooting": 94,
        "passing": 82,
        "dribbling": 90,
        "defending": 35,
        "physicality": 78,
        "skill_moves": 5,
        "weak_foot": 4,
        "foot": "Right",
        "accelerate_type": "Explosive",
        "image_url": "https://cdn.example.com/img/12345.png",
        "card_bg_image": "https://cdn.example.com/bg/gold.png",
        "card_cutout_image": "https://cdn.example.com/cutout/12345.png",
        "card_cutout_type": "base",
        "nation": "Portugal",
        "nation_image": "https://cdn.example.com/nation/portugal.png",
        "club": "Al Nassr",
        "club_image": "https://cdn.example.com/club/al-nassr.png",
        "league": "Saudi Pro League",
        "league_image": "https://cdn.example.com/league/spl.png",
        # Volatile fields that must NOT affect the hash.
        "price": "25,000",
        "price_num": 25000,
        "price_updated_at": "2026-07-29T00:00:00Z",
        "games_played_console": 12,
    }
    row.update(overrides)
    return row


class TestRenderHash:
    def test_hash_is_deterministic_for_identical_input(self):
        row = _base_row()
        assert compute_card_render_hash(row) == compute_card_render_hash(dict(row))

    def test_hash_is_stable_regardless_of_key_order(self):
        row = _base_row()
        shuffled = dict(reversed(list(row.items())))
        assert compute_card_render_hash(row) == compute_card_render_hash(shuffled)

    @pytest.mark.parametrize(
        "field,new_value",
        [
            ("rating", 92),
            ("position", "CF"),
            ("altposition", "CF"),
            ("pace", 88),
            ("skill_moves", 4),
            ("weak_foot", 3),
            ("foot", "Left"),
            ("card_bg_image", "https://cdn.example.com/bg/other.png"),
            ("card_cutout_image", "https://cdn.example.com/cutout/other.png"),
            ("card_cutout_type", "special"),
            ("nation_image", "https://cdn.example.com/nation/other.png"),
            ("club_image", "https://cdn.example.com/club/other.png"),
            ("league_image", "https://cdn.example.com/league/other.png"),
            ("version", "TOTW"),
            ("card_name", "CR7"),
        ],
    )
    def test_hash_changes_when_a_visible_field_changes(self, field, new_value):
        original = compute_card_render_hash(_base_row())
        changed = compute_card_render_hash(_base_row(**{field: new_value}))
        assert original != changed

    @pytest.mark.parametrize(
        "field,new_value",
        [
            ("price", "99,999"),
            ("price_num", 99999),
            ("price_updated_at", "2030-01-01T00:00:00Z"),
            ("games_played_console", 999),
        ],
    )
    def test_hash_does_not_change_for_volatile_price_fields(self, field, new_value):
        original = compute_card_render_hash(_base_row())
        changed = compute_card_render_hash(_base_row(**{field: new_value}))
        assert original == changed

    def test_hash_changes_when_render_version_bumps(self, monkeypatch):
        import app.services.player_card_hash as mod

        row = _base_row()
        original = compute_card_render_hash(row)
        monkeypatch.setattr(mod, "PLAYER_CARD_RENDER_VERSION", PLAYER_CARD_RENDER_VERSION + 1)
        bumped = mod.compute_card_render_hash(row)
        assert original != bumped

    def test_hash_is_a_64_char_hex_sha256_digest(self):
        digest = compute_card_render_hash(_base_row())
        assert len(digest) == 64
        int(digest, 16)  # raises ValueError if not valid hex


class TestRenderToken:
    def test_valid_token_round_trips(self):
        token = make_render_token("12345")
        assert verify_render_token("12345", token) is True

    def test_token_rejected_for_a_different_card_id(self):
        token = make_render_token("12345")
        assert verify_render_token("99999", token) is False

    def test_empty_or_garbage_token_rejected(self):
        assert verify_render_token("12345", "") is False
        assert verify_render_token("12345", "not-a-real-token") is False

    def test_expired_token_rejected(self):
        # max_age=0 would race real wall-clock time between mint and
        # check, so assert against a negative max_age instead - that's
        # unambiguously always "expired" regardless of timing.
        token = make_render_token("12345")
        assert verify_render_token("12345", token, max_age_seconds=-1) is False


class TestStorageKeySafety:
    def test_upload_rejects_empty_buffer(self):
        from app.services.object_storage import _validate_upload

        with pytest.raises(ObjectStorageError):
            _validate_upload("fc26/generated-player-cards/1/abc123.png", b"")

    def test_upload_rejects_path_traversal_key(self):
        from app.services.object_storage import _validate_upload

        with pytest.raises(ObjectStorageError):
            _validate_upload("../../etc/passwd", b"not-empty")

    def test_upload_rejects_absolute_path_key(self):
        from app.services.object_storage import _validate_upload

        with pytest.raises(ObjectStorageError):
            _validate_upload("/etc/passwd", b"not-empty")

    def test_upload_accepts_a_normal_versioned_key(self):
        from app.services.object_storage import _validate_upload

        _validate_upload("fc26/generated-player-cards/12345/abc123def456.png", b"not-empty")
