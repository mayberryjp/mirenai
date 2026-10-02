"""In-memory buffer of individual DNS query/response events.

The resolver appends one event per query (name, type, rcode, answer) and a
background timer flushes the batch to the repository, which bulk-inserts the
rows and prunes anything past the retention window. Unlike the aggregating
query-log buffer this keeps every event (no counting), so it is bounded by
``max_events`` and drops the oldest events if a flush falls behind.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mirenai.logging import get_logger

log = get_logger("dns.queryevents")

# Upper bound on events held between flushes; oldest are dropped past this.
_MAX_EVENTS = 50000


@dataclass(frozen=True)
class QueryEvent:
    client: str
    domain: str
    qtype: str
    rcode: str
    response: str
    created_at: datetime


QueryEventFlush = Callable[[list[QueryEvent]], None]


class QueryEventBuffer:
    def __init__(
        self, flush: QueryEventFlush, flush_seconds: int, max_events: int = _MAX_EVENTS
    ) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._events: deque[QueryEvent] = deque(maxlen=max_events)
        self._stop = threading.Event()

    def add(self, client: str, domain: str, qtype: str, rcode: str, response: str) -> None:
        # Timestamp at capture time, not flush time, so the window query is accurate.
        event = QueryEvent(
            client=client,
            domain=domain,
            qtype=qtype,
            rcode=rcode,
            response=response,
            created_at=datetime.now(),
        )
        with self._lock:
            self._events.append(event)

    def flush(self) -> None:
        with self._lock:
            if not self._events:
                return
            snapshot = list(self._events)
            self._events.clear()
        try:
            self._flush_fn(snapshot)
        except Exception:
            log.exception("query event flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="query-event-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
