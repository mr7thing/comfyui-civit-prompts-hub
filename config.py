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


def get_proxy():
    """Outbound proxy URL, or None for a direct connection.

    Checked in order: explicit ``proxy`` in config.json, then the Windows
    internet proxy setting (aiohttp does not read that on its own).
    """
    configured = load_config().get("proxy")
    if configured:
        return str(configured)

    return _windows_proxy()


def _windows_proxy():
    try:
        import winreg
    except ImportError:
        return None

    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        )
        with key:
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                enabled = 0
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
    except OSError:
        return None

    server = str(server).strip()
    if not server:
        return None

    # Many setups (proxy clients with the system toggle off) still keep a
    # loopback proxy recorded here, and that is the one that actually works.
    if not enabled and not _is_loopback(server):
        return None

    if not server.startswith(("http://", "https://", "socks5://")):
        server = f"http://{server}"
    return server


def _is_loopback(server):
    host = server.split("//")[-1].split(":")[0]
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")



def public_state():
    """Config state safe to send to the frontend. Never includes the token."""
    cfg = load_config()
    return {
        "has_token": has_token(),
        "token_source": (
            "env" if os.environ.get(TOKEN_ENV_VAR) else ("file" if has_token() else None)
        ),
        "browsing_level": cfg.get("browsing_level"),
        "proxy": get_proxy(),
    }
