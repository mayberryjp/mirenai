"""Client for the Sando network-inventory API.

When ``SANDO_API_URL`` is set, mirenai can borrow the friendly name and icon that
Sando already keeps for a client IP. Sando exposes a per-IP lookup at
``GET {base}/api/localhosts/{ip}`` returning a JSON object; its
``local_description`` maps to the host's ``device_name`` and its ``icon`` maps to
the host's ``icon``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from mirenai.config import settings
from mirenai.logging import get_logger
from mirenai.repository.hosts import update_host_by_ip

log = get_logger("integrations.sando")

_HTTP_TIMEOUT = 5
_USER_AGENT = "mirenai/0.1 (Sando host sync; contact: /u/homelabids)"


class SandoError(Exception):
    """Raised when the Sando API cannot be reached or returns an unexpected reply."""


@dataclass(frozen=True)
class SandoDevice:
    device_name: str | None
    icon: str | None


def _clean(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    trimmed = value.strip()
    return trimmed or None


def fetch_device(ip: str) -> SandoDevice | None:
    """Look up a client IP in Sando.

    Returns a :class:`SandoDevice` (either field may be ``None``) when Sando knows
    the host, or ``None`` when Sando has no record for it (HTTP ``404``). Raises
    :class:`SandoError` when Sando is unreachable or replies unexpectedly.
    """
    base = settings.sando_api_url.rstrip("/")
    url = f"{base}/api/localhosts/{ip}"
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise SandoError(f"unsupported Sando URL scheme: {parsed.scheme or 'none'}")
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    # Scheme is restricted to http/https above, so this is not an arbitrary-scheme open.
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            payload = json.loads(resp.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise SandoError(f"HTTP {exc.code}") from exc
    except (URLError, TimeoutError, ValueError, OSError) as exc:
        raise SandoError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("error"):
        return None
    return SandoDevice(
        device_name=_clean(payload.get("local_description")),
        icon=_clean(payload.get("icon")),
    )


def sync_host_from_sando(ip: str) -> dict[str, Any] | None:
    """Fetch ``ip`` from Sando and apply its name/icon to the stored host row.

    Returns the updated host dict, or ``None`` when Sando is not configured, has
    no record for the IP, supplies nothing useful, or the host row is absent.
    Raises :class:`SandoError` on transport/protocol failures.
    """
    if not settings.sando_api_url:
        return None
    device = fetch_device(ip)
    if device is None:
        return None
    data: dict[str, Any] = {}
    if device.device_name is not None:
        data["device_name"] = device.device_name
    if device.icon is not None:
        data["icon"] = device.icon
    if not data:
        return None
    return update_host_by_ip(ip, data)
