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
from datetime import datetime

from mirenai.domain.policy import ACTION_DENY
from mirenai.logging import get_logger

log = get_logger("dns.querybuffer")


@dataclass(frozen=True)
class QueryAgg:
    client: str
    domain: str
    qtype: str
    count: int
    last_action: str
    last_response: str
    denied_count: int = 0
    denied_first_seen: datetime | None = None
    denied_last_seen: datetime | None = None


@dataclass
class _Counter:
    count: int
    last_action: str
    last_response: str
    denied_count: int = 0
    denied_first_seen: datetime | None = None
    denied_last_seen: datetime | None = None


FlushCallback = Callable[[list[QueryAgg]], None]


class QueryBuffer:
    def __init__(self, flush: FlushCallback, flush_seconds: int) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, str], _Counter] = {}
        self._stop = threading.Event()

    def add(self, client: str, domain: str, qtype: str, action: str, response: str) -> None:
        key = (client, domain, qtype)
        with self._lock:
            counter = self._data.get(key)
            if counter is None:
                counter = _Counter(count=1, last_action=action, last_response=response)
                self._data[key] = counter
            else:
                counter.count += 1
                counter.last_action = action
                counter.last_response = response
            if action == ACTION_DENY:
                now = datetime.now()
                counter.denied_count += 1
                if counter.denied_first_seen is None:
                    counter.denied_first_seen = now
                counter.denied_last_seen = now

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
                last_response=counter.last_response,
                last_action=counter.last_action,
                denied_count=counter.denied_count,
                denied_first_seen=counter.denied_first_seen,
                denied_last_seen=counter.denied_last_seen,
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
