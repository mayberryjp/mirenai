"""In-memory aggregation of per-client DNS request objects (query names).

Counts each ``(client, domain, qtype)`` in memory and flushes aggregated rows to
the database on an hourly timer, where they upsert (``hits += n``, ``last_seen``
refreshed). Mirrors the query-log buffer but batches hourly into its own table.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from mirenai.logging import get_logger

log = get_logger("dns.clientrequests")


@dataclass(frozen=True)
class ClientRequestAgg:
    client: str
    domain: str
    qtype: str
    hits: int


ClientRequestFlush = Callable[[list[ClientRequestAgg]], None]


class ClientRequestBuffer:
    def __init__(self, flush: ClientRequestFlush, flush_seconds: int) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, str], int] = {}
        self._stop = threading.Event()

    def add(self, client: str, domain: str, qtype: str) -> None:
        key = (client, domain, qtype)
        with self._lock:
            self._data[key] = self._data.get(key, 0) + 1

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [
            ClientRequestAgg(client=key[0], domain=key[1], qtype=key[2], hits=hits)
            for key, hits in snapshot.items()
        ]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("client requests flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="client-requests-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
