"""In-memory aggregation buffer for the query log.

Writing a row per DNS query would put the database on the hot path. Instead the
resolver increments an in-memory counter keyed by ``(client, domain, qtype)`` and
a background timer flushes aggregated rows to the repository, which upserts them
(count += n, last_seen = now).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass

from mirenai.logging import get_logger

log = get_logger("dns.querybuffer")


@dataclass(frozen=True)
class QueryAgg:
    client: str
    domain: str
    qtype: str
    count: int
    last_action: str


@dataclass
class _Counter:
    count: int
    last_action: str


FlushCallback = Callable[[list[QueryAgg]], None]


class QueryBuffer:
    def __init__(self, flush: FlushCallback, flush_seconds: int) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, str], _Counter] = {}
        self._stop = threading.Event()

    def add(self, client: str, domain: str, qtype: str, action: str) -> None:
        key = (client, domain, qtype)
        with self._lock:
            counter = self._data.get(key)
            if counter is None:
                self._data[key] = _Counter(count=1, last_action=action)
            else:
                counter.count += 1
                counter.last_action = action

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [
            QueryAgg(
                client=key[0],
                domain=key[1],
                qtype=key[2],
                count=counter.count,
                last_action=counter.last_action,
            )
            for key, counter in snapshot.items()
        ]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("query log flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="query-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
