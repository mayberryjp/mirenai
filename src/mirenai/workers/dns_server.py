"""DNS server process.

Runs the screening resolver over both UDP and TCP, backed by an in-memory
policy/upstream/settings snapshot that refreshes on a background timer and an
aggregating query-log buffer. Started by supervisord as its own program.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from dnslib import RCODE
from dnslib.server import BaseResolver, DNSServer

from mirenai.config import settings
from mirenai.domain.cache import TTLCache
from mirenai.domain.clientrequests import ClientRequestBuffer
from mirenai.domain.clientstats import ClientStatsBuffer
from mirenai.domain.hosts import HostTracker
from mirenai.domain.querybuffer import QueryBuffer
from mirenai.domain.resolver import DnsResolver
from mirenai.domain.state import RuntimeState
from mirenai.logging import configure_logging, get_logger
from mirenai.repository.blocklists import load_blocklist_domains
from mirenai.repository.client_requests import record_client_requests
from mirenai.repository.client_stats import record_client_stats
from mirenai.repository.hosts import load_known_hosts, record_hosts
from mirenai.repository.policies import load_rules
from mirenai.repository.query_log import record_queries
from mirenai.repository.settings import load_runtime_settings
from mirenai.repository.upstreams import load_upstreams

log = get_logger("dns.server")

_DB_RETRY_SECONDS = 3
_STATS_FLUSH_SECONDS = 3600
_REQUESTS_FLUSH_SECONDS = 3600


class ScreeningResolver(BaseResolver):  # type: ignore[misc]  # dnslib is untyped
    def __init__(self, core: DnsResolver, hosts: HostTracker) -> None:
        self._core = core
        self._hosts = hosts

    def resolve(self, request: Any, handler: Any) -> Any:
        client_ip = handler.client_address[0]
        self._hosts.record(client_ip)
        try:
            return self._core.handle(request, client_ip)
        except Exception:
            log.exception("failed to resolve query from %s", client_ip)
            reply = request.reply()
            reply.header.rcode = RCODE.SERVFAIL
            return reply


def _build_state() -> RuntimeState:
    """Load initial state, retrying until the database schema is ready."""
    while True:
        try:
            return RuntimeState(
                load_settings=load_runtime_settings,
                load_policies=load_rules,
                load_upstreams=load_upstreams,
                load_blocklist=load_blocklist_domains,
            )
        except Exception:
            log.warning("database not ready; retrying in %ds", _DB_RETRY_SECONDS)
            time.sleep(_DB_RETRY_SECONDS)


def main() -> None:
    configure_logging(settings.log_level)

    state = _build_state()
    runtime = state.settings
    cache: TTLCache[bytes] = TTLCache(runtime.cache_max_entries)
    buffer = QueryBuffer(flush=record_queries, flush_seconds=runtime.query_flush_seconds)
    stats = ClientStatsBuffer(flush=record_client_stats, flush_seconds=_STATS_FLUSH_SECONDS)
    requests = ClientRequestBuffer(
        flush=record_client_requests, flush_seconds=_REQUESTS_FLUSH_SECONDS
    )
    hosts = HostTracker(
        flush=record_hosts,
        load=load_known_hosts,
        flush_seconds=runtime.query_flush_seconds,
        refresh_seconds=runtime.refresh_seconds,
    )
    core = DnsResolver(state, cache, buffer, stats, requests)
    resolver = ScreeningResolver(core, hosts)

    udp_server = DNSServer(
        resolver, port=settings.dns_port, address=settings.dns_listen_address, tcp=False
    )
    tcp_server = DNSServer(
        resolver, port=settings.dns_port, address=settings.dns_listen_address, tcp=True
    )

    state.start_refresh()
    buffer.start()
    stats.start()
    requests.start()
    hosts.start()
    udp_server.start_thread()
    tcp_server.start_thread()
    log.info(
        "dns server listening on %s:%s (udp+tcp)",
        settings.dns_listen_address,
        settings.dns_port,
    )

    stop = threading.Event()
    try:
        while not stop.wait(3600):
            pass
    except KeyboardInterrupt:
        log.info("shutting down dns server")
    finally:
        udp_server.stop()
        tcp_server.stop()
        state.stop()
        buffer.stop()
        stats.stop()
        requests.stop()
        hosts.stop()


if __name__ == "__main__":
    main()
