"""In-memory aggregation of per-upstream forward round-trip time, bucketed by hour.

The resolver reports each successful forward's upstream address and round-trip
time in milliseconds; samples accumulate in memory keyed by ``(hour_start,
address)`` and a background timer flushes aggregated rows to the database. Just
before each timed flush the buffer runs an optional ``before_flush`` hook, which
the DNS worker uses to backfill a lone synthetic sample for any upstream still
missing from the current hour's bucket. Old buckets are purged by the repository
on write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime

from mirenai.logging import get_logger

log = get_logger("dns.upstreamstats")


@dataclass(frozen=True)
class UpstreamRttAgg:
    hour_start: datetime
    address: str
    samples: int
    total_ms: float
    max_ms: float


UpstreamRttFlush = Callable[[list[UpstreamRttAgg]], None]


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


@dataclass
class _Accumulator:
    samples: int
    total_ms: float
    max_ms: float


class UpstreamRttBuffer:
    def __init__(
        self,
        flush: UpstreamRttFlush,
        flush_seconds: int,
        before_flush: Callable[[], None] | None = None,
    ) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._before_flush = before_flush
        self._lock = threading.Lock()
        self._data: dict[tuple[datetime, str], _Accumulator] = {}
        self._covered_hour: datetime | None = None
        self._covered: set[str] = set()
        self._stop = threading.Event()

    def _mark_covered(self, hour: datetime, address: str) -> None:
        """Record that ``address`` has a measurement or probe attempt this hour."""
        if self._covered_hour != hour:
            self._covered_hour = hour
            self._covered = set()
        self._covered.add(address)

    def add(self, address: str, rtt_ms: float) -> None:
        hour = _floor_hour(datetime.now())
        with self._lock:
            self._mark_covered(hour, address)
            acc = self._data.get((hour, address))
            if acc is None:
                self._data[(hour, address)] = _Accumulator(1, rtt_ms, rtt_ms)
            else:
                acc.samples += 1
                acc.total_ms += rtt_ms
                acc.max_ms = max(acc.max_ms, rtt_ms)

    def mark_probed(self, address: str) -> None:
        """Mark ``address`` as handled this hour without recording a sample.

        Lets the worker suppress a repeat synthetic probe within the hour even when
        the probe failed, so an unreachable upstream is contacted at most once.
        """
        hour = _floor_hour(datetime.now())
        with self._lock:
            self._mark_covered(hour, address)

    def uncovered(self, addresses: Iterable[str]) -> list[str]:
        """Return the ``addresses`` with no measurement or probe attempt this hour."""
        hour = _floor_hour(datetime.now())
        with self._lock:
            covered = self._covered if self._covered_hour == hour else set()
            return [address for address in addresses if address not in covered]

    def flush(self) -> None:
        with self._lock:
            if not self._data:
                return
            snapshot = self._data
            self._data = {}
        rows = [
            UpstreamRttAgg(
                hour_start=hour,
                address=address,
                samples=acc.samples,
                total_ms=acc.total_ms,
                max_ms=acc.max_ms,
            )
            for (hour, address), acc in snapshot.items()
        ]
        try:
            self._flush_fn(rows)
        except Exception:
            log.exception("upstream rtt flush failed")

    def start(self) -> None:
        thread = threading.Thread(target=self._loop, name="upstream-rtt-flush", daemon=True)
        thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self._flush_seconds):
            if self._before_flush is not None:
                try:
                    self._before_flush()
                except Exception:
                    log.exception("upstream rtt pre-flush hook failed")
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
