# app/services/object_storage.py
#
# Thin S3-compatible object storage client for permanent PNG storage.
# Nothing like this existed anywhere in the codebase before (confirmed:
# no boto3, no S3_*/AWS_S3_* env vars, no Supabase/R2 client) - this is
# the first storage integration. Works unmodified against real AWS S3
# (leave S3_ENDPOINT unset) or any S3-compatible provider such as
# Cloudflare R2 (set S3_ENDPOINT to the account's R2 endpoint and
# S3_FORCE_PATH_STYLE=1).
#
# boto3 is sync-only; every call here runs in a worker thread via
# asyncio.to_thread so it doesn't block the event loop the rest of this
# asyncpg/aiohttp-based app relies on.
from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from typing import Optional

import boto3
from botocore.config import Config as BotoConfig


class ObjectStorageError(RuntimeError):
    pass


def _env(name: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
    v = os.getenv(name, default)
    if required and not v:
        raise ObjectStorageError(f"Missing required env var: {name}")
    return v


@lru_cache(maxsize=1)
def _client():
    region = _env("AWS_REGION", required=False, default="auto")
    endpoint = os.getenv("S3_ENDPOINT") or None
    path_style = (os.getenv("S3_FORCE_PATH_STYLE", "0") or "0").lower() in ("1", "true", "yes")

    return boto3.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint,
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY"),
        config=BotoConfig(s3={"addressing_style": "path" if path_style else "auto"}),
    )


def _bucket() -> str:
    return _env("AWS_S3_BUCKET")


def _public_base_url() -> str:
    base = _env("AWS_PUBLIC_BASE_URL")
    return base.rstrip("/")


def _put_object_sync(key: str, data: bytes, content_type: str) -> None:
    _client().put_object(
        Bucket=_bucket(),
        Key=key,
        Body=data,
        ContentType=content_type,
        CacheControl="public, max-age=31536000, immutable",
    )


def _validate_upload(key: str, data: bytes) -> None:
    """Split out from upload_png so this validation (the only part worth
    unit-testing without a live S3-compatible endpoint) doesn't need an
    async test runner - see tests/test_player_card_pipeline.py."""
    if not data:
        raise ObjectStorageError("Refusing to upload an empty PNG buffer")
    if not key or ".." in key or key.startswith("/"):
        raise ObjectStorageError(f"Refusing to upload to unsafe storage key: {key!r}")


async def upload_png(key: str, data: bytes) -> str:
    """Uploads an immutable PNG and returns its public URL."""
    _validate_upload(key, data)
    await asyncio.to_thread(_put_object_sync, key, data, "image/png")
    return f"{_public_base_url()}/{key}"
