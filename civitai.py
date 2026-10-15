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

# Authoritative enum, taken from the API itself: sending an unknown value makes
# /images answer 400 with a ZodError listing every accepted option.
BASE_MODELS = (
    "Anima", "AuraFlow", "Chroma", "CogVideoX", "Ernie", "Flux.1 S", "Flux.1 D",
    "Flux.1 Krea", "Flux.1 Kontext", "Flux.2 D", "Flux.2 Klein 9B",
    "Flux.2 Klein 9B-base", "Flux.2 Klein 4B", "Flux.2 Klein 4B-base", "Flux.3",
    "Flux 3 Video", "Grok", "HappyHorse", "HiDream", "HiDream-O1", "Hunyuan 1",
    "Hunyuan Video", "Ideogram 4.0", "Ideogram 4.5", "Boogu", "Illustrious",
    "Imagen4", "Kolors", "Krea 2", "Krea 2 Turbo", "Krea 3", "LTXV", "LTXV2",
    "LTXV 2.3", "LTXV 2.5", "Lens", "Lumina", "MageFlow", "MAI", "Mochi",
    "Nano Banana", "NoobAI", "ODOR", "OpenAI", "Upscaler", "Other", "PixArt a",
    "PixArt E", "Playground v2", "Pony", "Pony V7", "Qwen", "Qwen 2", "Qwen 2.1",
    "Qwen 3", "Stable Cascade", "SD 1.4", "SD 1.5", "SD 1.5 LCM",
    "SD 1.5 Hyper", "SD 2.0", "SD 2.0 768", "SD 2.1", "SD 2.1 768",
    "SD 2.1 Unclip", "SD 3", "SD 3.5", "SD 3.5 Large", "SD 3.5 Large Turbo",
    "SD 3.5 Medium", "SDXL 0.9", "SDXL 1.0", "SDXL 1.0 LCM", "SDXL Lightning",
    "SDXL Hyper", "SDXL Turbo", "SDXL Distilled", "Reve", "Muse Image",
    "Seedream", "SVD", "SVD XT", "Sora 2", "Veo 3", "Wan Video",
    "Wan Video 1.3B t2v", "Wan Video 14B t2v", "Wan Video 14B i2v 480p",
    "Wan Video 14B i2v 720p", "Wan Video 2.2 TI2V-5B", "Wan Video 2.2 I2V-A14B",
    "Wan Video 2.2 T2V-A14B", "Wan Video 2.5 T2V", "Wan Video 2.5 I2V",
    "Wan Image 2.7", "Wan Video 2.7", "Wan Video 3.0", "ZImageTurbo",
    "ZImageBase", "Ming Image Design 0.1", "Ming Image Design Layer 0.1",
    "Vidu Q1", "MiniMax H3", "Kling", "Seedance", "ACE Audio", "MiniMax Music 3",
    "YuE2", "Sonilo", "PolyGen", "Tripo", "Hunyuan3D", "Pixal3D", "Trellis.2",
)

BASE_MODEL_SET = frozenset(BASE_MODELS)

# Our own curation of the still-image bases. The full enum above also holds
# video, audio and 3D entries, which are noise in an image gallery filter.
IMAGE_BASE_MODELS = (
    "Anima", "AuraFlow", "Chroma", "Ernie", "Flux.1 S", "Flux.1 D", "Flux.1 Krea",
    "Flux.1 Kontext", "Flux.2 D", "Flux.2 Klein 9B", "Flux.2 Klein 9B-base",
    "Flux.2 Klein 4B", "Flux.2 Klein 4B-base", "Flux.3", "Grok", "HappyHorse",
    "HiDream", "HiDream-O1", "Ideogram 4.0", "Ideogram 4.5", "Boogu",
    "Illustrious", "Imagen4", "Kolors", "Krea 2", "Krea 2 Turbo", "Krea 3",
    "Lens", "Lumina", "MageFlow", "MAI", "Nano Banana", "NoobAI", "ODOR",
    "OpenAI", "PixArt a", "PixArt E", "Playground v2", "Pony", "Pony V7",
    "Qwen", "Qwen 2", "Qwen 2.1", "Qwen 3", "SD 1.4", "SD 1.5", "SD 1.5 LCM",
    "SD 1.5 Hyper", "SD 2.0", "SD 2.0 768", "SD 2.1", "SD 2.1 768",
    "SD 2.1 Unclip", "SD 3", "SD 3.5", "SD 3.5 Large", "SD 3.5 Large Turbo",
    "SD 3.5 Medium", "SDXL 0.9", "SDXL 1.0", "SDXL 1.0 LCM", "SDXL Lightning",
    "SDXL Hyper", "SDXL Turbo", "SDXL Distilled", "Reve", "Muse Image",
    "Seedream", "Upscaler", "Other", "Wan Image 2.7", "ZImageTurbo", "ZImageBase",
    "Ming Image Design 0.1", "Ming Image Design Layer 0.1",
)


def normalise_base_models(value):
    """Split and validate baseModels input, raising ValueError on junk.

    Returns a list of accepted enum values, preserving the API's own order.
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        raw = [str(v).strip() for v in value]
    else:
        raw = [part.strip() for part in str(value).split(",")]

    invalid = [v for v in raw if v and v not in BASE_MODEL_SET]
    if invalid:
        raise ValueError(
            f"未知的 base model：{', '.join(invalid)}。"
            "取值必须和 Civitai 完全一致（注意空格与大小写），例如 Krea 2 不是 krea2。"
        )

    chosen = {v for v in raw if v}
    return [model for model in BASE_MODELS if model in chosen]


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
            if key == "baseModels":
                models = normalise_base_models(value)
                if models:
                    params[key] = ",".join(models)
            else:
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


async def fetch_images(
    session, filters, token=None, with_meta=True, max_retries=3, proxy=None, prepared=None
):
    """Fetch one page of images. Returns ``(items, next_cursor, next_page)``.

    ``prepared`` lets a caller validate ``build_params`` up front so an invalid
    filter surfaces as a clean 400 instead of a retry loop.
    """
    params = prepared if prepared is not None else build_params(filters, with_meta=with_meta)
    url = f"{API_ROOT}/images"
    last_error = None

    for attempt in range(max_retries):
        try:
            async with session.get(
                url, params=params, headers=_headers(token), proxy=proxy
            ) as resp:
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


async def download(session, url, proxy=None, max_bytes=64 * 1024 * 1024):
    """Fetch an image body, refusing anything larger than ``max_bytes``."""
    async with session.get(url, proxy=proxy, allow_redirects=True) as resp:
        if resp.status != 200:
            raise CivitaiError(f"Download failed ({resp.status})", resp.status)

        declared = resp.headers.get("Content-Length")
        if declared and declared.isdigit() and int(declared) > max_bytes:
            raise CivitaiError(f"Image too large: {int(declared) // 1024 // 1024} MB")

        body = bytearray()
        async for chunk in resp.content.iter_chunked(256 * 1024):
            body.extend(chunk)
            if len(body) > max_bytes:
                raise CivitaiError("Image exceeded the size limit while downloading")

        return bytes(body)


def extension_for(url):
    """Best-effort file extension from a Civitai CDN url."""
    path = (url or "").split("?")[0]
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return suffix if suffix in ("jpg", "jpeg", "png", "webp", "gif", "avif") else "jpg"

