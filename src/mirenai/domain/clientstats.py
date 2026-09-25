"""In-memory aggregation of per-client DNS query stats, bucketed by hour.

The resolver reports each query's client and result; counts accumulate in memory
keyed by ``(hour_start, client)`` and a background timer flushes aggregated rows
to the database once an hour. Old buckets are purged by the repository on write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mirenai.logging import get_logger

log = get_logger("dns.clientstats")

# resolver ``result`` string -> stat column name
_RESULT_COLUMNS = {
    "forward": "forwarded",
    "forward-cache": "cached",
    "override": "overridden",
    "deny": "denied",
    "blocklist": "blocked",
    "servfail": "servfail",
}


@dataclass(frozen=True)
class ClientStatAgg:
    hour_start: datetime
    client: str
    total: int
    forwarded: int
    cached: int
    overridden: int
    denied: int
    blocked: int
    servfail: int


ClientStatsFlush = Callable[[list[ClientStatAgg]], None]


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


class ClientStatsBuffer:
    def __init__(self, flush: ClientStatsFlush, flush_seconds: int) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[datetime, str], dict[str, int]] = {}
        self._stop = threading.Event()

    def add(self, client: str, result: str) -> None:
        hour = _floor_hour(datetime.now())
        column = _RESULT_COLUMNS.get(result)
        with self._lock:
            counter = self._data.setdefault((hour, client), {})
            counter["total"] = counter.get("total", 0) + 1
            if column is not None:
                counter[column] = counter.get(column, 0) + 1

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [
            ClientStatAgg(
                hour_start=hour,
                client=client,
                total=counter.get("total", 0),
                forwarded=counter.get("forwarded", 0),
                cached=counter.get("cached", 0),
                overridden=counter.get("overridden", 0),
                denied=counter.get("denied", 0),
                blocked=counter.get("blocked", 0),
                servfail=counter.get("servfail", 0),
            )
            for (hour, client), counter in snapshot.items()
        ]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("client stats flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="client-stats-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
