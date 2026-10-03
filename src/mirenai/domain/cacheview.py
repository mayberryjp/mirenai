"""Decode the DNS worker's in-memory answer cache into snapshot rows.

The cache stores packed upstream replies keyed by ``name|qtype|qclass`` (the
qtype/qclass are the numeric wire values). To surface the cache over the API the
worker turns each live entry into a :class:`CacheEntrySnapshot` — human-readable
record type/class plus the answer rdata parsed back out of the packed reply —
which the repository then persists for ``GET /cache``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from dnslib import CLASS, QTYPE, DNSRecord

from mirenai.domain.cache import TTLCache
from mirenai.logging import get_logger

log = get_logger("dns.cacheview")


@dataclass(frozen=True)
class CacheEntrySnapshot:
    domain: str
    qtype: str
    qclass: str
    response: str
    answers: int
    ttl: int
    expires_at: datetime


def _qtype_name(value: str) -> str:
    try:
        return str(QTYPE[int(value)])
    except Exception:
        return value


def _qclass_name(value: str) -> str:
    try:
        return str(CLASS[int(value)])
    except Exception:
        return value


def _split_key(key: str) -> tuple[str, str, str]:
    # Domain names never contain '|', so split the two trailing fields off the right.
    parts = key.rsplit("|", 2)
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    return key, "", ""


def build_cache_snapshot(cache: TTLCache[bytes]) -> list[CacheEntrySnapshot]:
    """Decode every live cache entry into a persistable snapshot row."""
    now = datetime.now()
    rows: list[CacheEntrySnapshot] = []
    for key, value, remaining, ttl in cache.snapshot():
        domain, qtype, qclass = _split_key(key)
        try:
            reply = DNSRecord.parse(value)
            response = ", ".join(str(record.rdata) for record in reply.rr)
            answers = len(reply.rr)
        except Exception:
            log.warning("failed to parse cached reply for %s", key)
            response = ""
            answers = 0
        rows.append(
            CacheEntrySnapshot(
                domain=domain,
                qtype=_qtype_name(qtype),
                qclass=_qclass_name(qclass),
                response=response,
                answers=answers,
                ttl=ttl,
                expires_at=now + timedelta(seconds=remaining),
            )
        )
    return rows
