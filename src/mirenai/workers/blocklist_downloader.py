"""Blocklist downloader process.

Periodically fetches each enabled blocklist whose configured update interval has
elapsed, parses it, and replaces that list's domains in the blocklist database.
The DNS server picks the new domains up on its own refresh timer. Started by
supervisord as its own program; the download step is also reused by the API's
manual-refresh endpoint.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from mirenai.config import settings
from mirenai.domain.blocklist import parse_blocklist
from mirenai.logging import configure_logging, get_logger
from mirenai.repository import blocklists as repo

log = get_logger("blocklist.downloader")

_POLL_SECONDS = 60
_DB_RETRY_SECONDS = 3
_HTTP_TIMEOUT = 30
_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB guard against a runaway download
_USER_AGENT = "mirenai/0.1 (DNS blocklist fetcher; contact: /u/homelabids)"


class BlocklistDownloadError(Exception):
    """Raised when a blocklist cannot be downloaded."""


def _describe(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        return str(exc.reason)
    return f"{type(exc).__name__}: {exc}"


def download_text(url: str) -> str:
    """Fetch a blocklist over HTTP(S) and return its decoded text."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise BlocklistDownloadError(f"unsupported URL scheme: {parsed.scheme or 'none'}")
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    # Scheme is restricted to http/https above, so this is not an arbitrary-scheme open.
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            raw: bytes = resp.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise BlocklistDownloadError(_describe(exc)) from exc
    if len(raw) > _MAX_BYTES:
        raise BlocklistDownloadError(f"blocklist exceeds {_MAX_BYTES} bytes")
    return raw.decode("utf-8-sig", errors="replace")


def refresh_blocklist(blocklist_id: int) -> dict[str, Any]:
    """Download, parse, and store one blocklist; return its updated config row.

    Records the outcome (domain count or error) on the blocklist row. On failure
    the previously stored domains are left untouched and the error is re-raised.
    """
    row = repo.get_blocklist(blocklist_id)
    if row is None:
        raise BlocklistDownloadError("blocklist not found")
    try:
        text = download_text(row["url"])
    except BlocklistDownloadError as exc:
        repo.record_failure(blocklist_id, str(exc))
        raise
    domains = parse_blocklist(text)
    repo.replace_domains(blocklist_id, domains)
    repo.record_success(blocklist_id, len(domains))
    return repo.get_blocklist(blocklist_id) or row


def _is_due(row: dict[str, Any], now: datetime) -> bool:
    if not row["enabled"]:
        return False
    last = row["last_downloaded_at"]
    if last is None:
        return True
    try:
        last_dt = datetime.fromisoformat(last)
    except ValueError:
        return True
    return now - last_dt >= timedelta(hours=row["update_interval_hours"])


def run_due_downloads() -> None:
    now = datetime.now()
    for row in repo.list_blocklists():
        if not _is_due(row, now):
            continue
        try:
            updated = refresh_blocklist(row["id"])
            log.info(
                "blocklist '%s' updated: %s domains", row["name"], updated["domain_count"]
            )
        except BlocklistDownloadError as exc:
            log.warning("blocklist '%s' download failed: %s", row["name"], exc)


def _wait_for_db() -> None:
    while True:
        try:
            repo.count_blocklists()
            return
        except Exception:
            log.warning("database not ready; retrying in %ds", _DB_RETRY_SECONDS)
            time.sleep(_DB_RETRY_SECONDS)


def main() -> None:
    configure_logging(settings.log_level)
    _wait_for_db()
    log.info("blocklist downloader started (poll every %ds)", _POLL_SECONDS)

    stop = threading.Event()
    try:
        while not stop.is_set():
            try:
                run_due_downloads()
            except Exception:
                log.exception("blocklist refresh cycle failed")
            stop.wait(_POLL_SECONDS)
    except KeyboardInterrupt:
        log.info("shutting down blocklist downloader")


if __name__ == "__main__":
    main()
