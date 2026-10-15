"""Minimal async client for the Civitai images API.

Reference: https://developer.civitai.com/site/reference/images.md

Pagination notes that matter here:
  - ``cursor`` is opaque; pass ``metadata.nextCursor`` back verbatim and keep
    every other query parameter identical.
  - an empty ``items`` array is not the end of results, only a missing
    ``nextCursor`` is.
  - a ``feed:`` cursor returns ``503`` with ``Retry-After: 2`` when overloaded.
"""

import asyncio

import aiohttp

API_ROOT = "https://civitai.com/api/v1"
MAX_LIMIT = 200

# Query parameters accepted by GET /images. Anything else is dropped so the
# cursor stays continuable.
PARAM_KEYS = (
    "cursor",
    "postId",
    "modelId",
    "modelVersionId",
    "imageId",
    "username",
    "userId",
    "period",
    "sort",
    "nsfw",
    "browsingLevel",
    "tags",
    "type",
    "baseModels",
)

SORTS = ("Most Reactions", "Most Comments", "Most Collected", "Newest", "Oldest", "Random")
PERIODS = ("Day", "Week", "Month", "Year", "AllTime")
MEDIA_TYPES = ("image", "video", "audio")


class CivitaiError(Exception):
    """A non-retryable error returned by the Civitai API."""

    def __init__(self, message, status=None, payload=None):
        super().__init__(message)
        self.status = status
        self.payload = payload


class CursorExpired(CivitaiError):
    """The cursor can no longer be continued; start over without it."""


def build_params(filters, with_meta=True):
    """Normalise a filter dict into API query parameters."""
    params = {"limit": _clamp_limit(filters.get("limit"))}

    if with_meta:
        params["withMeta"] = "true"

    for key in PARAM_KEYS:
        if key == "cursor":
            continue
        value = filters.get(key)
        if value is None or value == "":
            continue
        if key == "period":
            params[key] = value if value in PERIODS else "AllTime"
        elif key == "sort":
            params[key] = value if value in SORTS else "Newest"
        elif key == "type":
            params[key] = value if value in MEDIA_TYPES else "image"
        elif key in ("tags", "baseModels"):
            if isinstance(value, (list, tuple, set)):
                value = ",".join(str(v).strip() for v in value if str(v).strip())
            else:
                value = str(value).strip()
            if value:
                params[key] = value
        elif key in ("modelId", "modelVersionId", "postId", "imageId", "userId", "browsingLevel"):
            params[key] = int(value)
        else:
            params[key] = str(value)

    cursor = filters.get("cursor")
    if cursor:
        params["cursor"] = cursor

    return params


def _clamp_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 50
    return max(1, min(MAX_LIMIT, limit))


def _headers(token):
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def fetch_images(session, filters, token=None, with_meta=True, max_retries=3):
    """Fetch one page of images. Returns ``(items, next_cursor, next_page)``."""
    params = build_params(filters, with_meta=with_meta)
    url = f"{API_ROOT}/images"
    last_error = None

    for attempt in range(max_retries):
        try:
            async with session.get(url, params=params, headers=_headers(token)) as resp:
                if resp.status == 200:
                    body = await resp.json()
                    meta = body.get("metadata") or {}
                    return (
                        body.get("items") or [],
                        meta.get("nextCursor"),
                        meta.get("nextPage"),
                    )

                if resp.status == 503:
                    # Transient overload; retry the same cursor unchanged.
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        delay = float(retry_after)
                    except (TypeError, ValueError):
                        delay = 2.0
                    last_error = CivitaiError("Civitai is temporarily overloaded", 503)
                    await asyncio.sleep(delay)
                    continue

                payload = await resp.json(content_type=None)
                message = _error_message(payload) or f"HTTP {resp.status}"
                if resp.status == 400 and "start again" in message.lower():
                    raise CursorExpired(message, resp.status, payload)
                raise CivitaiError(message, resp.status, payload)

        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            last_error = CivitaiError(f"Network error: {exc}")
            await asyncio.sleep(min(2 ** attempt, 5))

    raise last_error or CivitaiError("Failed to fetch images")


def _error_message(payload):
    if isinstance(payload, dict):
        for key in ("message", "error"):
            value = payload.get(key)
            if value:
                return str(value)
    return None


def new_session():
    timeout = aiohttp.ClientTimeout(total=60, connect=15)
    return aiohttp.ClientSession(timeout=timeout, raise_for_status=False)
