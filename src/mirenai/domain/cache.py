"""In-memory TTL cache with LRU eviction.

The DNS resolver stores packed upstream responses keyed by
``name|qtype|qclass``. Expiry is tracked with a monotonic clock so wall-clock
changes never affect cached lifetimes, and the remaining lifetime is used to
count TTLs down on the way back out (see the resolver).
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass
class CacheEntry(Generic[T]):
    value: T
    inserted_at: float
    expires_at: float
    ttl: int


class TTLCache(Generic[T]):
    def __init__(self, max_entries: int) -> None:
        self._max_entries = max(1, max_entries)
        self._data: OrderedDict[str, CacheEntry[T]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: str) -> CacheEntry[T] | None:
        now = time.monotonic()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if entry.expires_at <= now:
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return entry

    def set(self, key: str, value: T, ttl: int) -> None:
        now = time.monotonic()
        with self._lock:
            self._data[key] = CacheEntry(
                value=value,
                inserted_at=now,
                expires_at=now + ttl,
                ttl=ttl,
            )
            self._data.move_to_end(key)
            while len(self._data) > self._max_entries:
                self._data.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)
