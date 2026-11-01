"""Bounded, thread-safe capture of first-seen notification candidates."""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from dnslib import QTYPE

from mirenai.logging import get_logger

log = get_logger("dns.notifications")


def default_query_type_flags() -> dict[str, bool]:
    return {name: True for name in sorted(QTYPE.reverse)}


def normalize_query_type_flags(flags: dict[str, bool]) -> dict[str, bool]:
    normalized: dict[str, bool] = {}
    for name, enabled in flags.items():
        canonical = name.strip().upper()
        if canonical not in QTYPE.reverse:
            if (
                not canonical.startswith("TYPE") or not canonical[4:].isascii()
                or not canonical[4:].isdigit()
            ):
                raise ValueError("discord_query_types keys must be DNS query types (e.g. A, AAAA, PTR)")
            number = int(canonical[4:])
            if not 0 <= number <= 65535:
                raise ValueError("DNS query type numbers must be between 0 and 65535")
            canonical = str(QTYPE[number])
        if canonical in normalized:
            raise ValueError("discord_query_types contains duplicate normalized query types")
        normalized[canonical] = enabled
    return normalized


@dataclass(frozen=True)
class DomainObservation:
    client: str
    domain: str
    qtype: str
    action: str
    rcode: str
    first_seen: datetime
    notifications_enabled: bool
    query_type_enabled: bool = True


class NotificationBuffer:
    def __init__(
        self, flush: Callable[[list[DomainObservation]], None], max_events: int = 10000
    ) -> None:
        self._flush = flush
        self._max_events = max_events
        self._lock = threading.Lock()
        self._events: dict[tuple[str, str, str, bool], DomainObservation] = {}

    def add(
        self, client: str, domain: str, qtype: str, action: str, rcode: str,
        notifications_enabled: bool,
        query_type_enabled: bool = True,
    ) -> None:
        key = (client, domain, qtype, query_type_enabled)
        with self._lock:
            if key in self._events:
                return
            if len(self._events) >= self._max_events:
                log.warning("notification capture full; dropped client=%s domain=%s", client, domain)
                return
            self._events[key] = DomainObservation(
                client, domain, qtype, action, rcode, datetime.now(),
                notifications_enabled, query_type_enabled,
            )

    def flush(self) -> None:
        with self._lock:
            if not self._events:
                return
            snapshot = self._events
            self._events = {}
        try:
            self._flush(list(snapshot.values()))
        except Exception:
            log.exception("notification observation persistence failed; retaining batch")
            with self._lock:
                # Older observations must win, including after a failed transaction.
                combined = snapshot | {
                    key: event for key, event in self._events.items() if key not in snapshot
                }
                if len(combined) > self._max_events:
                    log.warning("notification retry buffer full; dropping newest observations")
                self._events = dict(list(combined.items())[:self._max_events])
