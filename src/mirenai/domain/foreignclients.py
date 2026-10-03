"""In-memory aggregation of dropped foreign-source IPs.

Counts each untrusted source IP in memory and flushes aggregated rows to the
database on a timer, where they upsert (``hits += n``, ``last_seen`` refreshed).
The number of distinct IPs held between flushes is capped so a flood of spoofed
source addresses can't grow memory without bound: once the cap is reached, hits
for already-tracked IPs keep counting but previously-unseen IPs are skipped (they
remain counted in the aggregate ``foreign`` stat).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from mirenai.logging import get_logger

log = get_logger("dns.foreignclients")

# Max distinct IPs buffered between flushes (bounds memory against spoof floods).
DEFAULT_MAX_CLIENTS = 1024


@dataclass(frozen=True)
class ForeignClientAgg:
    ip: str
    hits: int


ForeignClientFlush = Callable[[list[ForeignClientAgg]], None]


class ForeignClientBuffer:
    def __init__(
        self,
        flush: ForeignClientFlush,
        flush_seconds: int,
        max_clients: int = DEFAULT_MAX_CLIENTS,
    ) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._max_clients = max(1, max_clients)
        self._lock = threading.Lock()
        self._data: dict[str, int] = {}
        self._stop = threading.Event()

    def add(self, ip: str) -> None:
        with self._lock:
            if ip in self._data:
                self._data[ip] += 1
            elif len(self._data) < self._max_clients:
                self._data[ip] = 1

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [ForeignClientAgg(ip=ip, hits=hits) for ip, hits in snapshot.items()]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("foreign clients flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="foreign-clients-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
