"""In-memory aggregation of per-upstream forward round-trip time, bucketed by hour.

The resolver reports each successful forward's upstream address and round-trip
time in milliseconds; samples accumulate in memory keyed by ``(hour_start,
address)`` and a background timer flushes aggregated rows to the database. Old
buckets are purged by the repository on write.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
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
    def __init__(self, flush: UpstreamRttFlush, flush_seconds: int) -> None:
        self._flush_fn = flush
        self._flush_seconds = max(1, flush_seconds)
        self._lock = threading.Lock()
        self._data: dict[tuple[datetime, str], _Accumulator] = {}
        self._stop = threading.Event()

    def add(self, address: str, rtt_ms: float) -> None:
        hour = _floor_hour(datetime.now())
        with self._lock:
            acc = self._data.get((hour, address))
            if acc is None:
                self._data[(hour, address)] = _Accumulator(1, rtt_ms, rtt_ms)
            else:
                acc.samples += 1
                acc.total_ms += rtt_ms
                acc.max_ms = max(acc.max_ms, rtt_ms)

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
            self.flush()

    def stop(self) -> None:
        self._stop.set()
        self.flush()
