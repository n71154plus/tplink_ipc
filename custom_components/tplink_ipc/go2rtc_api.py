"""Minimal go2rtc HTTP API client (host-network localhost)."""

from __future__ import annotations

import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

_LOGGER = logging.getLogger(__name__)

DEFAULT_GO2RTC = "http://127.0.0.1:1984"


class Go2rtcError(Exception):
    """go2rtc API failure."""


def _request(method: str, url: str, *, timeout: float = 8.0) -> bytes:
    req = urllib.request.Request(url, method=method, data=b"")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", "replace") if err.fp else ""
        raise Go2rtcError(f"{method} {url} -> {err.code}: {body}") from err
    except urllib.error.URLError as err:
        raise Go2rtcError(f"{method} {url} failed: {err}") from err


def patch_stream(
    name: str,
    src: str,
    *,
    base_url: str = DEFAULT_GO2RTC,
) -> None:
    """Create/replace a runtime stream (does not persist go2rtc.yaml)."""
    q = urllib.parse.urlencode({"name": name, "src": src})
    _request("PATCH", f"{base_url.rstrip('/')}/api/streams?{q}")
    _LOGGER.debug("go2rtc PATCH stream %s -> %s", name, src)


def delete_stream(name: str, *, base_url: str = DEFAULT_GO2RTC) -> None:
    q = urllib.parse.urlencode({"src": name})
    try:
        _request("DELETE", f"{base_url.rstrip('/')}/api/streams?{q}")
    except Go2rtcError as err:
        _LOGGER.debug("go2rtc DELETE %s: %s", name, err)


def stream_info(name: str, *, base_url: str = DEFAULT_GO2RTC) -> dict[str, Any]:
    q = urllib.parse.urlencode({"src": name})
    raw = _request("GET", f"{base_url.rstrip('/')}/api/streams?{q}")
    import json

    return json.loads(raw.decode() or "{}")
