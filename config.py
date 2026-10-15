"""Plugin configuration.

The Civitai API token is read from, in order of precedence:
  1. environment variable CIVITAI_API_TOKEN
  2. config.json next to this file

The token is never exposed through node widgets or exported workflows.
"""

import json
import os
from pathlib import Path

CONFIG_PATH = Path(__file__).parent / "config.json"
TOKEN_ENV_VAR = "CIVITAI_API_TOKEN"

_cache = None


def load_config():
    global _cache
    if _cache is None:
        data = {}
        if CONFIG_PATH.is_file():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
        _cache = data if isinstance(data, dict) else {}
    return _cache


def get_token():
    """Return the configured API token, or None when unauthenticated."""
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        token = load_config().get("api_token")
    return token or None


def has_token():
    return get_token() is not None


def public_state():
    """Config state safe to send to the frontend. Never includes the token."""
    cfg = load_config()
    return {
        "has_token": has_token(),
        "token_source": (
            "env" if os.environ.get(TOKEN_ENV_VAR) else ("file" if has_token() else None)
        ),
        "browsing_level": cfg.get("browsing_level"),
    }
