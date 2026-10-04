"""Parsing and in-memory lookup for locally-served DNS records.

A *local zone* is a plain-text file (typically a raw GitHub URL) of comma-separated
``value,name`` lines that the resolver answers authoritatively, as if they were real
DNS records. Each line expands into one or more :class:`LocalRecord`:

* ``192.0.2.10,host.example.lan`` -> an **A** record (``host.example.lan`` ->
  ``192.0.2.10``) **and** a **PTR** record for the reverse lookup of the address.
* ``2001:db8::10,host.example.lan`` -> an **AAAA** record plus its **PTR**.
* ``host.example.lan,alias.example.lan`` -> a **CNAME** (``alias.example.lan`` ->
  ``host.example.lan``) when the first field is a name rather than an IP address.

An optional third field sets the record TTL (seconds); otherwise :data:`DEFAULT_TTL`
is used. ``#`` starts a comment (full-line or inline); blank lines are ignored.

This module is pure (no ``dnslib`` / database); the resolver turns the records it
produces into wire answers and the repository persists them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import ip_address

from mirenai.domain.policy import normalize_domain

DEFAULT_TTL = 300

RTYPE_A = "A"
RTYPE_AAAA = "AAAA"
RTYPE_CNAME = "CNAME"
RTYPE_PTR = "PTR"

# Record types this module can parse and the resolver can serve.
VALID_RTYPES = frozenset({RTYPE_A, RTYPE_AAAA, RTYPE_CNAME, RTYPE_PTR})

# Label = 1-63 chars of letters/digits/hyphen/underscore, not starting/ending with a
# hyphen. A name is one or more labels, so single-label local names (e.g. ``nas``) and
# reverse-DNS names (``10.0.168.192.in-addr.arpa``) are both accepted.
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
_NAME_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})*$")


@dataclass(frozen=True)
class LocalRecord:
    """One locally-served DNS record (already normalized and expanded)."""

    name: str
    rtype: str
    value: str
    ttl: int = DEFAULT_TTL


def _is_valid_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.match(name))


def _parse_ttl(raw: str, default_ttl: int) -> int:
    try:
        return max(0, int(raw))
    except ValueError:
        return default_ttl


def _expand(value: str, name: str, ttl: int) -> list[LocalRecord]:
    """Expand one ``value,name`` pair into its forward and reverse records."""
    try:
        addr = ip_address(value)
    except ValueError:
        target = normalize_domain(value)
        if not _is_valid_name(target):
            return []
        return [LocalRecord(name=name, rtype=RTYPE_CNAME, value=target, ttl=ttl)]
    rtype = RTYPE_A if addr.version == 4 else RTYPE_AAAA
    return [
        LocalRecord(name=name, rtype=rtype, value=str(addr), ttl=ttl),
        LocalRecord(name=addr.reverse_pointer, rtype=RTYPE_PTR, value=name, ttl=ttl),
    ]


def parse_zone(text: str, default_ttl: int = DEFAULT_TTL) -> list[LocalRecord]:
    """Parse zone-file ``text`` into unique, expanded local records.

    Lines are ``value,name[,ttl]``. The ``value`` is an IPv4/IPv6 address (producing
    an A/AAAA forward record and a PTR reverse record) or a hostname (producing a
    CNAME). Comments (``#``), blank lines, and syntactically invalid rows are skipped.
    """
    records: list[LocalRecord] = []
    seen: set[tuple[str, str, str]] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2 or not parts[0] or not parts[1]:
            continue
        ttl = _parse_ttl(parts[2], default_ttl) if len(parts) >= 3 and parts[2] else default_ttl
        name = normalize_domain(parts[1])
        if not _is_valid_name(name):
            continue
        for record in _expand(parts[0], name, ttl):
            key = (record.name, record.rtype, record.value)
            if key in seen:
                continue
            seen.add(key)
            records.append(record)
    return records


class LocalRecords:
    """Immutable name/type index over local records for O(1) resolver lookups."""

    def __init__(self, records: Iterable[LocalRecord]) -> None:
        index: dict[tuple[str, str], list[LocalRecord]] = {}
        names: set[str] = set()
        count = 0
        for record in records:
            index.setdefault((record.name, record.rtype), []).append(record)
            names.add(record.name)
            count += 1
        self._index = index
        self._names = frozenset(names)
        self._count = count

    def owns(self, name: str) -> bool:
        """Whether any local record exists for ``name`` (so it is served authoritatively)."""
        return name in self._names

    def get(self, name: str, rtype: str) -> list[LocalRecord]:
        """Return the records for ``(name, rtype)`` (empty when none)."""
        return self._index.get((name, rtype), [])

    def cname(self, name: str) -> LocalRecord | None:
        """Return the CNAME record for ``name`` if one is defined."""
        records = self._index.get((name, RTYPE_CNAME))
        return records[0] if records else None

    def __len__(self) -> int:
        return self._count
