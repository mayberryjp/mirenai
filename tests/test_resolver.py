from dnslib import RCODE, DNSRecord

from mirenai.domain.cache import TTLCache
from mirenai.domain.clientrequests import ClientRequestBuffer
from mirenai.domain.clientstats import ClientStatsBuffer
from mirenai.domain.policy import PolicyRule
from mirenai.domain.querybuffer import QueryAgg, QueryBuffer
from mirenai.domain.resolver import DnsResolver
from mirenai.domain.state import RuntimeSettings, RuntimeState, UpstreamServer


def _make_resolver(
    policies: list[PolicyRule],
    settings: RuntimeSettings | None = None,
    upstreams: list[UpstreamServer] | None = None,
    blocklist: frozenset[str] | None = None,
) -> DnsResolver:
    effective = settings or RuntimeSettings()
    state = RuntimeState(
        load_settings=lambda: effective,
        load_policies=lambda: policies,
        load_upstreams=lambda: upstreams or [],
        load_blocklist=lambda: blocklist or frozenset(),
    )
    buffer = QueryBuffer(flush=lambda rows: None, flush_seconds=5)
    stats = ClientStatsBuffer(flush=lambda rows: None, flush_seconds=3600)
    requests = ClientRequestBuffer(flush=lambda rows: None, flush_seconds=3600)
    cache: TTLCache[bytes] = TTLCache(100)
    return DnsResolver(state, cache, buffer, stats, requests)


def test_deny_returns_nxdomain() -> None:
    resolver = _make_resolver([PolicyRule("*", "*", "deny")])
    reply = resolver.handle(DNSRecord.question("blocked.example", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.NXDOMAIN


def test_default_action_denies_unmatched() -> None:
    resolver = _make_resolver([], RuntimeSettings(default_action="deny"))
    reply = resolver.handle(DNSRecord.question("unknown.example", "A"), "9.9.9.9")
    assert reply.header.rcode == RCODE.NXDOMAIN


def test_override_returns_configured_a_record() -> None:
    resolver = _make_resolver([PolicyRule("*", "ads.example", "override", "0.0.0.0", 60)])
    reply = resolver.handle(DNSRecord.question("ads.example", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 1
    assert str(reply.rr[0].rdata) == "0.0.0.0"
    assert reply.rr[0].ttl == 60


def test_forward_without_upstreams_is_servfail() -> None:
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
    )
    reply = resolver.handle(DNSRecord.question("example.com", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.SERVFAIL


def test_blocklist_action_denies_listed_domain() -> None:
    resolver = _make_resolver(
        [PolicyRule("*", "*", "blocklist")],
        RuntimeSettings(cache_enabled=False),
        blocklist=frozenset({"ads.example"}),
    )
    reply = resolver.handle(DNSRecord.question("ads.example", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.NXDOMAIN


def test_blocklist_action_denies_subdomain_of_listed_domain() -> None:
    resolver = _make_resolver(
        [PolicyRule("*", "*", "blocklist")],
        RuntimeSettings(cache_enabled=False),
        blocklist=frozenset({"example.com"}),
    )
    reply = resolver.handle(DNSRecord.question("tracker.example.com", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.NXDOMAIN


def test_blocklist_action_forwards_unlisted_domain() -> None:
    # Not on the list -> falls through to forward, which SERVFAILs without upstreams.
    resolver = _make_resolver(
        [PolicyRule("*", "*", "blocklist")],
        RuntimeSettings(cache_enabled=False),
        blocklist=frozenset({"ads.example"}),
    )
    reply = resolver.handle(DNSRecord.question("good.example", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.SERVFAIL


def test_query_buffer_aggregates_counts() -> None:
    captured: list[QueryAgg] = []
    buffer = QueryBuffer(flush=captured.extend, flush_seconds=5)
    buffer.add("1.2.3.4", "example.com", "A", "deny")
    buffer.add("1.2.3.4", "example.com", "A", "deny")
    buffer.add("1.2.3.4", "other.com", "AAAA", "forward")
    buffer.flush()
    by_key = {(row.client, row.domain, row.qtype): row for row in captured}
    assert by_key[("1.2.3.4", "example.com", "A")].count == 2
    assert by_key[("1.2.3.4", "other.com", "AAAA")].count == 1
