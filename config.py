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

    Resolution order:
      1. ``proxy_enabled: false`` in config.json forces a direct connection
      2. ``proxy`` in config.json
      3. the Windows internet proxy setting (aiohttp does not read that itself)
    """
    cfg = load_config()

    enabled = cfg.get("proxy_enabled")
    if enabled is False:
        return None

    configured = cfg.get("proxy")
    if configured:
        return str(configured)

    return _windows_proxy()


def detected_proxy():
    """The proxy found in the Windows settings, ignoring our own overrides."""
    return _windows_proxy()


def _write_config(data):
    CONFIG_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    global _cache
    _cache = data


def _read_config_file():
    if not CONFIG_PATH.is_file():
        return {}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_proxy(enabled, url=None):
    """Persist the proxy choice. Writes config.json, creating it if needed."""
    cfg = load_config()
    cfg["proxy_enabled"] = bool(enabled)

    if url is not None:
        cleaned = _normalise_proxy_url(str(url).strip())
        if cleaned:
            cfg["proxy"] = cleaned
        else:
            cfg.pop("proxy", None)

    data = _read_config_file()
    data["proxy_enabled"] = cfg["proxy_enabled"]
    if "proxy" in cfg:
        data["proxy"] = cfg["proxy"]
    else:
        data.pop("proxy", None)

    _write_config(data)
    return get_proxy()


def save_token(token):
    """Persist the API token so it survives a browser session."""
    cleaned = str(token or "").strip()
    data = _read_config_file()
    if cleaned:
        data["api_token"] = cleaned
    else:
        data.pop("api_token", None)
    _write_config(data)
    return bool(cleaned)



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


def _normalise_proxy_url(value):
    """Normalise a user-typed proxy address.

    The scheme matters more than it looks: aiohttp treats ``https://`` as "do a
    TLS handshake with the proxy itself", so pointing it at a plain HTTP proxy
    (Clash/xray default) fails with a confusing connect error.
    """
    if not value:
        return ""

    if not value.startswith(("http://", "https://", "socks5://", "socks5h://")):
        value = f"http://{value}"

    if value.startswith("https://") and _is_loopback(value):
        # Every local proxy listener we know of speaks plaintext; assume a typo
        # rather than failing later with a TLS error.
        value = "http://" + value[len("https://"):]

    return value




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
        "proxy_detected": detected_proxy(),
        "proxy_enabled": cfg.get("proxy_enabled", True),
        "proxy_configured": cfg.get("proxy"),
    }
