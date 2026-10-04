"""Local-zone downloader process.

Periodically fetches each enabled local zone whose configured update interval has
elapsed, parses its ``value,name`` lines into DNS records, and replaces that
zone's stored records. The DNS server picks the new records up on its own refresh
timer and serves them authoritatively. Started by supervisord as its own program;
the download step is also reused by the API's manual-refresh endpoint.
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
from mirenai.domain.localzones import parse_zone
from mirenai.logging import configure_logging, get_logger
from mirenai.repository import local_zones as repo

log = get_logger("localzones.downloader")

_POLL_SECONDS = 60
_DB_RETRY_SECONDS = 3
_HTTP_TIMEOUT = 30
_MAX_BYTES = 8 * 1024 * 1024  # 8 MiB guard against a runaway download
_USER_AGENT = "mirenai/0.1 (DNS local-zone fetcher; contact: /u/homelabids)"


class LocalZoneDownloadError(Exception):
    """Raised when a local zone cannot be downloaded."""


def _describe(exc: Exception) -> str:
    if isinstance(exc, HTTPError):
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        return str(exc.reason)
    return f"{type(exc).__name__}: {exc}"


def download_text(url: str) -> str:
    """Fetch a local-zone file over HTTP(S) and return its decoded text."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise LocalZoneDownloadError(f"unsupported URL scheme: {parsed.scheme or 'none'}")
    request = Request(url, headers={"User-Agent": _USER_AGENT})
    # Scheme is restricted to http/https above, so this is not an arbitrary-scheme open.
    try:
        with urlopen(request, timeout=_HTTP_TIMEOUT) as resp:  # nosec B310
            raw: bytes = resp.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise LocalZoneDownloadError(_describe(exc)) from exc
    if len(raw) > _MAX_BYTES:
        raise LocalZoneDownloadError(f"local zone exceeds {_MAX_BYTES} bytes")
    return raw.decode("utf-8-sig", errors="replace")


def refresh_local_zone(zone_id: int) -> dict[str, Any]:
    """Download, parse, and store one local zone; return its updated config row.

    Records the outcome (record count or error) on the zone row. On failure the
    previously stored records are left untouched and the error is re-raised.
    """
    row = repo.get_local_zone(zone_id)
    if row is None:
        raise LocalZoneDownloadError("local zone not found")
    try:
        text = download_text(row["url"])
    except LocalZoneDownloadError as exc:
        repo.record_failure(zone_id, str(exc))
        raise
    records = parse_zone(text)
    repo.replace_records(zone_id, records)
    repo.record_success(zone_id, len(records))
    return repo.get_local_zone(zone_id) or row


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
    return now - last_dt >= timedelta(seconds=row["update_interval_seconds"])


def run_due_downloads() -> None:
    now = datetime.now()
    for row in repo.list_local_zones():
        if not _is_due(row, now):
            continue
        try:
            updated = refresh_local_zone(row["id"])
            log.info(
                "local zone '%s' updated: %s records", row["name"], updated["record_count"]
            )
        except LocalZoneDownloadError as exc:
            log.warning("local zone '%s' download failed: %s", row["name"], exc)


def _wait_for_db() -> None:
    while True:
        try:
            repo.count_local_zones()
            return
        except Exception:
            log.warning("database not ready; retrying in %ds", _DB_RETRY_SECONDS)
            time.sleep(_DB_RETRY_SECONDS)


def main() -> None:
    configure_logging(settings.log_level)
    _wait_for_db()
    log.info("local-zone downloader started (poll every %ds)", _POLL_SECONDS)

    stop = threading.Event()
    try:
        while not stop.is_set():
            try:
                run_due_downloads()
            except Exception:
                log.exception("local-zone refresh cycle failed")
            stop.wait(_POLL_SECONDS)
    except KeyboardInterrupt:
        log.info("shutting down local-zone downloader")


if __name__ == "__main__":
    main()
