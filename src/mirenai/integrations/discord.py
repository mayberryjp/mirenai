"""Discord webhook transport. Webhook tokens must never appear in logs."""

from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from mirenai.domain.notifications import DomainObservation

_WEBHOOK = re.compile(
    r"https://(?:discord\.com|discordapp\.com)/api(?:/v[0-9]+)?/webhooks/[0-9]+/[A-Za-z0-9_-]+"
)


def validate_webhook_url(value: str) -> str:
    value = value.strip()
    if value and _WEBHOOK.fullmatch(value) is None:
        raise ValueError("discord_webhook_url must be an HTTPS Discord webhook URL")
    return value


class DiscordDeliveryError(Exception):
    def __init__(self, reason: str, retry_seconds: float | None = None) -> None:
        super().__init__(reason)
        self.retry_seconds = retry_seconds


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Request, fp: object, code: int, msg: str,
        headers: object, newurl: str,
    ) -> None:
        return None


def send_notification(
    url: str, event: DomainObservation, *, client_name: str | None = None,
) -> None:
    validate_webhook_url(url)
    if not url:
        raise ValueError("discord_webhook_url is required")
    if event.action in {"deny", "blocklist"}:
        outcome, icon = "BLOCK", "\U0001f6d1"
    elif event.rcode not in {"NOERROR", "NXDOMAIN"}:
        outcome, icon = "ERROR", "\u26a0\ufe0f"
    else:
        outcome, icon = "ALLOW", "\u2705"
    name = " ".join((client_name or "").split())[:255]
    name = re.sub(r"([\\`*_{}\[\]()<>~|])", r"\\\1", name)
    client = f"{name} ({event.client})" if name else event.client
    content = (
        f"{icon} **{outcome}**: **{client}** queried **{event.domain}** "
        f"for the first time ({event.qtype}; action: {event.action}; "
        f"DNS: {event.rcode}; first seen: {event.first_seen.isoformat()})."
    )
    payload = json.dumps({
        "content": content[:2000],
        "allowed_mentions": {"parse": []},
    }).encode("utf-8")
    request = Request(
        url, data=payload, method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "mirenai /u/homelabids",
        },
    )
    try:
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            if not 200 <= response.status < 300:
                raise DiscordDeliveryError(f"HTTP {response.status}")
    except HTTPError as exc:
        status = exc.code
        retry_seconds = None
        if status == 429:
            try:
                retry_seconds = min(3600.0, max(1.0, float(exc.headers.get("Retry-After", "5"))))
            except (TypeError, ValueError):
                retry_seconds = 5.0
        exc.close()
        if status == 429 or status >= 500:
            raise DiscordDeliveryError(f"HTTP {status}", retry_seconds or 5.0) from None
        raise DiscordDeliveryError(f"HTTP {status}") from None
    except (URLError, OSError):
        raise DiscordDeliveryError("network error", 5.0) from None
