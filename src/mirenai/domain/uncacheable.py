"""In-memory aggregation of forwarded answers that could not be cached.

Counts each ``(domain, qtype, reason)`` the resolver declined to cache and flushes
aggregated rows to the database on a timer, where they upsert (``hits += n``,
``last_seen``/``last_ttl`` refreshed). Surfaces *why* answers aren't cached so a
low cache-hit rate can be diagnosed. The number of distinct keys held between
flushes is capped so a flood of unique names (e.g. NXDOMAIN spam) can't grow
memory without bound: once the cap is reached, hits for already-tracked keys keep
counting but previously-unseen keys are skipped.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from mirenai.logging import get_logger

log = get_logger("dns.uncacheable")

# Max distinct (domain, qtype, reason) keys buffered between flushes.
DEFAULT_MAX_KEYS = 2048


@dataclass(frozen=True)
class UncacheableAgg:
    domain: str
    qtype: str
    reason: str
    last_ttl: int | None
    hits: int


UncacheableFlush = Callable[[list[UncacheableAgg]], None]


class UncacheableBuffer:
    def __init__(
        self,
        flush: UncacheableFlush,
        flush_seconds: int,
        max_keys: int = DEFAULT_MAX_KEYS,
    ) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._max_keys = max(1, max_keys)
        self._lock = threading.Lock()
        # (domain, qtype, reason) -> (hits, last_ttl)
        self._data: dict[tuple[str, str, str], tuple[int, int | None]] = {}
        self._stop = threading.Event()

    def add(self, domain: str, qtype: str, reason: str, ttl: int | None) -> None:
        key = (domain, qtype, reason)
        with self._lock:
            existing = self._data.get(key)
            if existing is not None:
                self._data[key] = (existing[0] + 1, ttl)
            elif len(self._data) < self._max_keys:
                self._data[key] = (1, ttl)

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [
            UncacheableAgg(
                domain=key[0], qtype=key[1], reason=key[2], last_ttl=ttl, hits=hits
            )
            for key, (hits, ttl) in snapshot.items()
        ]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("uncacheable flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="uncacheable-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
