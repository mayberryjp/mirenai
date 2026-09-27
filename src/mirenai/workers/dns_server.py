"""DNS server process.

Runs the screening resolver over both UDP and TCP, backed by an in-memory
policy/upstream/settings snapshot that refreshes on a background timer and an
aggregating query-log buffer. Started by supervisord as its own program.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
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
from mirenai.domain.upstreamstats import UpstreamRttBuffer
from mirenai.integrations.sando import sync_host_from_sando
from mirenai.logging import configure_logging, get_logger
from mirenai.repository.blocklists import load_blocklist_domains
from mirenai.repository.cache_control import get_cache_flush_request
from mirenai.repository.client_requests import materialize_new_domains, record_client_requests
from mirenai.repository.client_stats import record_client_stats
from mirenai.repository.hosts import load_blocklist_excluded, load_known_hosts, record_hosts
from mirenai.repository.policies import ensure_client_policy, load_rules
from mirenai.repository.query_log import record_queries
from mirenai.repository.runtime_stats import record_runtime_stats
from mirenai.repository.settings import load_runtime_settings
from mirenai.repository.upstream_stats import record_upstream_rtt
from mirenai.repository.upstreams import load_upstreams

log = get_logger("dns.server")

_DB_RETRY_SECONDS = 3
_STATS_FLUSH_SECONDS = 3600
_REQUESTS_FLUSH_SECONDS = 3600
_NEW_DOMAIN_MATERIALIZE_SECONDS = 3600


def _run_new_domain_materializer(stop: threading.Event) -> None:
    """Rebuild the dense new-domain hourly series immediately, then every hour."""
    while True:
        try:
            materialize_new_domains()
        except Exception:
            log.exception("new-domain materialize failed")
        if stop.wait(_NEW_DOMAIN_MATERIALIZE_SECONDS):
            return


def _runtime_snapshot(cache: TTLCache[bytes], state: RuntimeState) -> dict[str, int]:
    """Sample the current point-in-time runtime gauges."""
    return {
        "cache_size": len(cache),
        "cache_capacity": cache.capacity,
        "blocklist_domains": len(state.blocklist),
        "upstreams": len(state.upstreams),
        "policies": len(state.policies),
    }


def _run_runtime_stats(
    cache: TTLCache[bytes], state: RuntimeState, interval: int, stop: threading.Event
) -> None:
    """Flush the runtime-gauge snapshot immediately, then every ``interval`` seconds."""
    while True:
        try:
            record_runtime_stats(_runtime_snapshot(cache, state))
        except Exception:
            log.exception("runtime stats flush failed")
        if stop.wait(interval):
            return


def _run_cache_flusher(cache: TTLCache[bytes], interval: int, stop: threading.Event) -> None:
    """Clear the DNS cache whenever the API records a newer flush request."""
    try:
        last_seen = get_cache_flush_request()
    except Exception:
        last_seen = None
    while not stop.wait(interval):
        try:
            requested = get_cache_flush_request()
        except Exception:
            log.exception("cache flush poll failed")
            continue
        if requested is not None and requested != last_seen:
            cache.clear()
            last_seen = requested
            log.info("cache cleared (flush requested at %s)", requested)


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
                load_blocklist_excluded=load_blocklist_excluded,
            )
        except Exception:
            log.warning("database not ready; retrying in %ds", _DB_RETRY_SECONDS)
            time.sleep(_DB_RETRY_SECONDS)


def _make_on_discover(state: RuntimeState) -> Callable[[set[str]], None]:
    """Return a callback run when new clients are first seen.

    For each new client it seeds an explicit wildcard policy replicating the
    current site default and syncs the host's name/icon from Sando (if enabled).
    """

    def _on_discover(ips: set[str]) -> None:
        action = state.settings.default_action
        for ip in ips:
            try:
                ensure_client_policy(ip, action)
            except Exception:
                log.exception("failed to seed policy for new client %s", ip)
            try:
                sync_host_from_sando(ip)
            except Exception:
                log.exception("failed to sync new client %s from sando", ip)

    return _on_discover


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
    rtt = UpstreamRttBuffer(flush=record_upstream_rtt, flush_seconds=runtime.query_flush_seconds)
    hosts = HostTracker(
        flush=record_hosts,
        load=load_known_hosts,
        flush_seconds=runtime.query_flush_seconds,
        refresh_seconds=runtime.refresh_seconds,
        on_discover=_make_on_discover(state),
    )
    core = DnsResolver(state, cache, buffer, stats, requests, rtt)
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
    rtt.start()
    hosts.start()
    materialize_stop = threading.Event()
    threading.Thread(
        target=_run_new_domain_materializer,
        args=(materialize_stop,),
        name="new-domain-materializer",
        daemon=True,
    ).start()
    runtime_stats_stop = threading.Event()
    threading.Thread(
        target=_run_runtime_stats,
        args=(cache, state, runtime.query_flush_seconds, runtime_stats_stop),
        name="runtime-stats",
        daemon=True,
    ).start()
    cache_flush_stop = threading.Event()
    threading.Thread(
        target=_run_cache_flusher,
        args=(cache, runtime.refresh_seconds, cache_flush_stop),
        name="cache-flusher",
        daemon=True,
    ).start()
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
        rtt.stop()
        hosts.stop()
        materialize_stop.set()
        runtime_stats_stop.set()
        cache_flush_stop.set()


if __name__ == "__main__":
    main()
