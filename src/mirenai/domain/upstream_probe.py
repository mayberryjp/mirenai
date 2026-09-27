"""Round-trip timing for an upstream resolver via a sample DNS query."""

from __future__ import annotations

import time

from dnslib import DNSRecord

from mirenai.domain.state import UpstreamServer

# Innocuous, always-resolvable name used purely to measure the round trip.
_PROBE_DOMAIN = "example.com"


class ProbeError(Exception):
    """Raised when an upstream cannot be reached or returns an unparseable reply."""


def probe_upstream(server: UpstreamServer, timeout: float) -> float:
    """Send a sample A query through ``server`` and return the round-trip time in ms.

    Any reply (even an error rcode) counts as reachable. Raises :class:`ProbeError`
    on transport failure/timeout or if the reply cannot be parsed.
    """
    query = DNSRecord.question(_PROBE_DOMAIN, "A")
    start = time.perf_counter()
    try:
        raw = query.send(
            server.address,
            server.port,
            tcp=server.protocol == "tcp",
            timeout=timeout,
        )
        DNSRecord.parse(raw)
    except Exception as exc:
        raise ProbeError(str(exc)) from exc
    return round((time.perf_counter() - start) * 1000, 1)
