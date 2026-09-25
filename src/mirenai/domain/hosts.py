"""In-memory registry of client hosts seen by the DNS server.

Every query's source IP is recorded so operators can see which devices use the
resolver. Writing a row per query would put the hosts database on the hot path,
so per-IP counts are aggregated in memory and flushed on a timer. The set of
known hosts is likewise cached in memory and refreshed from the database.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

from mirenai.logging import get_logger

log = get_logger("dns.hosts")

HostFlush = Callable[[dict[str, int]], None]
HostLoader = Callable[[], set[str]]


class HostTracker:
    def __init__(
        self,
        flush: HostFlush,
        load: HostLoader,
        flush_seconds: int,
        refresh_seconds: int,
    ) -> None:
        self._flush_fn = flush
        self._load_fn = load
        self._flush_seconds = max(1, flush_seconds)
        self._refresh_seconds = max(1, refresh_seconds)
        self._lock = threading.Lock()
        self._pending: dict[str, int] = {}
        self._known: set[str] = set()
        self._stop = threading.Event()

    def record(self, ip: str) -> None:
        with self._lock:
            self._pending[ip] = self._pending.get(ip, 0) + 1
            self._known.add(ip)

    @property
    def known_hosts(self) -> set[str]:
        with self._lock:
            return set(self._known)

    def refresh(self) -> None:
        known = self._load_fn()
        with self._lock:
            # DB is the source of truth, plus any IPs seen but not yet flushed.
            self._known = known | set(self._pending)

    def flush(self) -> None:
        with self._lock:
            if not self._pending:
                return
            snapshot = self._pending
            self._pending = {}
        try:
            self._flush_fn(snapshot)
        except Exception:
            log.exception("host flush failed")

    def start(self) -> None:
        threading.Thread(target=self._flush_loop, name="host-flush", daemon=True).start()
        threading.Thread(target=self._refresh_loop, name="host-refresh", daemon=True).start()

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def _refresh_loop(self) -> None:
        # Load known hosts immediately on start, then refresh on the timer.
        while True:
            try:
                self.refresh()
            except Exception:
                log.exception("host refresh failed")
            if self._stop.wait(self._refresh_seconds):
                return

    def stop(self) -> None:
        self._stop.set()
        self.flush()
