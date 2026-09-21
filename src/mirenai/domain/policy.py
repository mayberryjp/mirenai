"""Pure policy-matching logic.

A policy row screens a single ``(client, domain)`` pair. Both columns support
wildcards so the three per-host modes collapse into one table:

* allow everything  -> ``(client, "*", forward)``
* deny everything   -> ``(client, "*", deny)``
* allow some        -> one row per allowed domain, with no wildcard row, so
  anything unmatched falls through to the configured default action (deny).

Domain column grammar:

* ``example.com``   exact name (case-insensitive, trailing dot ignored)
* ``*.example.com`` the name and any subdomain of ``example.com``
* ``*``             any domain

Client column grammar:

* ``203.0.113.10``  exact source address
* ``*``             any client
"""

from __future__ import annotations

from dataclasses import dataclass

WILDCARD = "*"

# Actions a matched rule can carry.
ACTION_FORWARD = "forward"
ACTION_OVERRIDE = "override"
ACTION_DENY = "deny"
VALID_ACTIONS = frozenset({ACTION_FORWARD, ACTION_OVERRIDE, ACTION_DENY})


@dataclass(frozen=True)
class PolicyRule:
    client: str
    domain: str
    action: str
    override_response: str | None = None
    override_ttl: int = 300


def normalize_domain(name: str) -> str:
    return name.rstrip(".").lower()


def _domain_specificity(rule_domain: str, qname: str) -> int | None:
    """Return a specificity score for a domain pattern, or ``None`` if no match.

    Higher scores are more specific: exact > longest suffix wildcard > ``*``.
    """
    pattern = rule_domain.rstrip(".").lower()
    if pattern == WILDCARD:
        return 0
    if pattern.startswith("*."):
        suffix = pattern[2:]
        if qname == suffix or qname.endswith("." + suffix):
            # Longer suffixes win over shorter ones.
            return 1000 + len(suffix)
        return None
    if pattern == qname:
        return 1_000_000
    return None


def _client_matches(rule_client: str, client_ip: str) -> bool:
    return rule_client == WILDCARD or rule_client == client_ip


def select_policy(rules: list[PolicyRule], client_ip: str, qname: str) -> PolicyRule | None:
    """Pick the most specific rule for ``(client_ip, qname)``.

    Precedence is ``(domain specificity, client specificity)`` so an exact client
    only breaks ties between domain patterns of equal specificity.
    """
    normalized = normalize_domain(qname)
    best: PolicyRule | None = None
    best_key: tuple[int, int] | None = None
    for rule in rules:
        if not _client_matches(rule.client, client_ip):
            continue
        specificity = _domain_specificity(rule.domain, normalized)
        if specificity is None:
            continue
        client_specificity = 1 if rule.client != WILDCARD else 0
        key = (specificity, client_specificity)
        if best_key is None or key > best_key:
            best_key = key
            best = rule
    return best
