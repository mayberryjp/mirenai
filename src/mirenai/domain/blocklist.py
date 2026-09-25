"""Blocklist parsing and matching.

Blocklists are plain text: one domain per line. Two common shapes are accepted:

* hosts format -- ``0.0.0.0 ads.example.com`` (a sink IP followed by the name)
* domain only  -- ``ads.example.com``

Lines beginning with ``#`` are comments, as is anything after an inline ``#``.
Blank lines, bare IP addresses, and syntactically invalid names are ignored.

A name is blocked if it exactly matches a listed domain or is a subdomain of
one, so listing ``example.com`` also covers ``ads.example.com``.
"""

from __future__ import annotations

import re
from ipaddress import ip_address

from mirenai.domain.policy import normalize_domain

# Label = 1-63 chars of letters/digits/hyphen/underscore, not starting/ending with a hyphen.
_LABEL = r"(?!-)[a-z0-9_-]{1,63}(?<!-)"
_DOMAIN_RE = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(\.{_LABEL})+$")


def _is_valid_domain(domain: str) -> bool:
    if not domain or "." not in domain:
        return False
    try:
        ip_address(domain)
        return False  # a bare IP address is not a domain to block
    except ValueError:
        pass
    return bool(_DOMAIN_RE.match(domain))


def parse_blocklist(text: str) -> list[str]:
    """Extract the unique, normalized domains from raw blocklist text."""
    domains: list[str] = []
    seen: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        # hosts format puts the sink IP first and the name second; otherwise the
        # whole (single) token is the name.
        candidate = parts[1] if len(parts) >= 2 else parts[0]
        domain = normalize_domain(candidate)
        if not _is_valid_domain(domain) or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


def is_blocked(blocked: frozenset[str], qname: str) -> bool:
    """Return ``True`` if ``qname`` or any of its parent domains is blocked."""
    name = normalize_domain(qname)
    if not name:
        return False
    labels = name.split(".")
    for i in range(len(labels)):
        if ".".join(labels[i:]) in blocked:
            return True
    return False
