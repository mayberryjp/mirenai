"""Blocklist parsing and matching.

Blocklists are plain text, one entry per line. Three common shapes are accepted:

* hosts format -- ``0.0.0.0 ads.example.com`` (a sink IP followed by the name)
* domain only  -- ``ads.example.com``
* Adblock/uBO  -- ``||ads.example.com^`` (host-anchor rules; a trailing ``$``
  modifier is dropped and the host is taken)

Comments start with ``#`` (hosts/domain) or ``!`` (Adblock), full-line or inline.
``[Adblock Plus]`` section headers, exception rules (``@@``), cosmetic filters
(``##``/``#?#``/``#@#``), and wildcard rules are ignored, as are blank lines, bare
IP addresses, and syntactically invalid names.

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
# ABP/uBO cosmetic-filter separators (##, #@#, #?#, #$#, #%#) -- never a DNS block.
_COSMETIC_RE = re.compile(r"#[@?$%]*#")


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
        domain = _extract_domain(raw_line)
        if domain is None or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
    return domains


def _extract_domain(raw_line: str) -> str | None:
    """Return the blockable domain a single blocklist line denotes, or ``None``."""
    line = raw_line.strip()
    if not line:
        return None
    # Adblock/uBO comment (!) and section ([Adblock Plus]) markers.
    if line[0] == "!" or (line.startswith("[") and line.endswith("]")):
        return None
    if line.startswith("||"):
        return _extract_adblock_domain(line)
    # Exception (allowlist) and cosmetic-filter rules never denote a blocked domain.
    if line.startswith("@@") or _COSMETIC_RE.search(line):
        return None
    # hosts/domain format: drop an inline '#' comment, then take the name token.
    line = line.split("#", 1)[0].strip()
    if not line:
        return None
    parts = line.split()
    # hosts format puts the sink IP first and the name second; otherwise the
    # whole (single) token is the name.
    candidate = parts[1] if len(parts) >= 2 else parts[0]
    domain = normalize_domain(candidate)
    return domain if _is_valid_domain(domain) else None


def _extract_adblock_domain(line: str) -> str | None:
    """Pull the host out of an Adblock host-anchor rule (``||host^$modifiers``)."""
    body = line[2:]  # drop the leading '||'
    cut = len(body)
    # The host ends at the first anchor (^), modifier ($), path (/), or wildcard (*).
    for terminator in ("^", "$", "/", "*"):
        idx = body.find(terminator)
        if idx != -1:
            cut = min(cut, idx)
    domain = normalize_domain(body[:cut])
    return domain if _is_valid_domain(domain) else None


FORMAT_HOSTS = "hosts"
FORMAT_DOMAIN = "domain"
FORMAT_ADBLOCK = "adblock"
FORMAT_UNKNOWN = "unknown"

_DETECT_SAMPLE_LIMIT = 50
# On a vote tie, prefer a recognized syntax over ``unknown``.
_FORMAT_PRIORITY = {FORMAT_HOSTS: 3, FORMAT_ADBLOCK: 3, FORMAT_DOMAIN: 2, FORMAT_UNKNOWN: 0}


def _classify_line(line: str) -> str:
    if line.startswith(("||", "@@")) or "^" in line:
        return FORMAT_ADBLOCK
    parts = line.split()
    if len(parts) >= 2:
        try:
            ip_address(parts[0])
        except ValueError:
            return FORMAT_UNKNOWN
        return FORMAT_HOSTS if _is_valid_domain(normalize_domain(parts[1])) else FORMAT_UNKNOWN
    if _is_valid_domain(normalize_domain(parts[0])):
        return FORMAT_DOMAIN
    return FORMAT_UNKNOWN


def detect_format(text: str) -> str:
    """Identify a blocklist's syntax from a sample of its entry lines.

    Returns ``"hosts"`` (``0.0.0.0 domain``), ``"domain"`` (one name per line),
    ``"adblock"`` (``||domain^`` Adblock/uBO syntax), or ``"unknown"`` when no
    entries are recognizable. Comment (``#``/``!``) and section (``[...]``) lines
    are ignored.
    """
    votes: dict[str, int] = {}
    sampled = 0
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line[0] in "#!" or (line.startswith("[") and line.endswith("]")):
            continue
        fmt = _classify_line(line)
        votes[fmt] = votes.get(fmt, 0) + 1
        sampled += 1
        if sampled >= _DETECT_SAMPLE_LIMIT:
            break
    if not votes:
        return FORMAT_UNKNOWN
    return max(votes, key=lambda fmt: (votes[fmt], _FORMAT_PRIORITY[fmt]))


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
