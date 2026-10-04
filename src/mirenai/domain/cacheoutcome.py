"""In-memory aggregation of forwarded-query cache outcomes, bucketed by hour.

The resolver reports the cache outcome of every forwarded query — ``cached`` when
the answer was stored, or the reason it wasn't (``nxdomain``, ``error``,
``nodata``, ``zero-ttl``, ``upstream-failure``). Counts accumulate in memory keyed
by ``(hour_start, reason)`` and a background timer flushes aggregated rows to the
database, where they upsert. Old buckets are purged by the repository on write.
The outcome set is small and fixed, so cardinality is naturally bounded.

This is independent of the per-client ``forwarded`` tally in ``clientstats`` — a
forwarded query is still counted there; this just records *why* its answer was or
wasn't cacheable, for a separate forwarded-reason chart.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from mirenai.logging import get_logger

log = get_logger("dns.cacheoutcome")

OUTCOME_CACHED = "cached"
OUTCOME_NXDOMAIN = "nxdomain"
OUTCOME_ERROR = "error"
OUTCOME_NODATA = "nodata"
OUTCOME_ZERO_TTL = "zero-ttl"
OUTCOME_UPSTREAM_FAILURE = "upstream-failure"

# Every bucket the forwarded cache-outcome chart can show (used to zero-fill the series).
CACHE_OUTCOME_REASONS = (
    OUTCOME_CACHED,
    OUTCOME_NXDOMAIN,
    OUTCOME_ERROR,
    OUTCOME_NODATA,
    OUTCOME_ZERO_TTL,
    OUTCOME_UPSTREAM_FAILURE,
)


@dataclass(frozen=True)
class CacheOutcomeAgg:
    hour_start: datetime
    reason: str
    hits: int


CacheOutcomeFlush = Callable[[list[CacheOutcomeAgg]], None]


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


class CacheOutcomeBuffer:
    def __init__(self, flush: CacheOutcomeFlush, flush_seconds: int) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[datetime, str], int] = {}
        self._stop = threading.Event()

    def add(self, reason: str) -> None:
        key = (_floor_hour(datetime.now()), reason)
        with self._lock:
            self._data[key] = self._data.get(key, 0) + 1

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [
            CacheOutcomeAgg(hour_start=hour, reason=reason, hits=hits)
            for (hour, reason), hits in snapshot.items()
        ]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("cache outcome flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="cache-outcome-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
