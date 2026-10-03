import pytest
from dnslib import QTYPE, RCODE, RR, A, DNSRecord

from mirenai.domain.cache import TTLCache
from mirenai.domain.clientrequests import ClientRequestBuffer
from mirenai.domain.clientstats import ClientStatAgg, ClientStatsBuffer
from mirenai.domain.foreignclients import ForeignClientAgg, ForeignClientBuffer
from mirenai.domain.policy import PolicyRule
from mirenai.domain.querybuffer import QueryAgg, QueryBuffer
from mirenai.domain.queryevents import QueryEvent, QueryEventBuffer
from mirenai.domain.resolver import DnsResolver
from mirenai.domain.state import RuntimeSettings, RuntimeState, UpstreamServer
from mirenai.domain.upstreamstats import UpstreamRttAgg, UpstreamRttBuffer


def _make_resolver(
    policies: list[PolicyRule],
    settings: RuntimeSettings | None = None,
    upstreams: list[UpstreamServer] | None = None,
    blocklist: frozenset[str] | None = None,
    blocklist_excluded: frozenset[str] | None = None,
    rtt: UpstreamRttBuffer | None = None,
    events: QueryEventBuffer | None = None,
    trusted_networks: list[str] | None = None,
    stats: ClientStatsBuffer | None = None,
    buffer: QueryBuffer | None = None,
    foreign: ForeignClientBuffer | None = None,
) -> DnsResolver:
    effective = settings or RuntimeSettings()
    state = RuntimeState(
        load_settings=lambda: effective,
        load_policies=lambda: policies,
        load_upstreams=lambda: upstreams or [],
        load_blocklist=lambda: blocklist or frozenset(),
        load_blocklist_excluded=lambda: blocklist_excluded or frozenset(),
        load_trusted_networks=lambda: trusted_networks or [],
    )
    buffer = buffer or QueryBuffer(flush=lambda rows: None, flush_seconds=5)
    stats_buffer = stats or ClientStatsBuffer(flush=lambda rows: None, flush_seconds=3600)
    requests = ClientRequestBuffer(flush=lambda rows: None, flush_seconds=3600)
    rtt_buffer = rtt or UpstreamRttBuffer(flush=lambda rows: None, flush_seconds=3600)
    events_buffer = events or QueryEventBuffer(flush=lambda rows: None, flush_seconds=3600)
    foreign_buffer = foreign or ForeignClientBuffer(flush=lambda rows: None, flush_seconds=3600)
    cache: TTLCache[bytes] = TTLCache(100)
    return DnsResolver(
        state, cache, buffer, stats_buffer, requests, rtt_buffer, events_buffer, foreign_buffer
    )


def test_is_trusted_honors_configured_subnets() -> None:
    resolver = _make_resolver([PolicyRule("*", "*", "forward")], trusted_networks=["10.2.10.0/24"])
    assert resolver.is_trusted("10.2.10.5") is True
    assert resolver.is_trusted("192.168.1.1") is False


def test_is_trusted_without_subnets_trusts_everyone() -> None:
    resolver = _make_resolver([PolicyRule("*", "*", "forward")])
    assert resolver.is_trusted("203.0.113.9") is True


def test_record_foreign_increments_foreign_stat() -> None:
    captured: list[ClientStatAgg] = []
    stats = ClientStatsBuffer(flush=captured.extend, flush_seconds=3600)
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")], trusted_networks=["10.2.10.0/24"], stats=stats
    )
    resolver.record_foreign("192.168.1.50")
    stats.flush()
    assert len(captured) == 1
    assert captured[0].client == "foreign"
    assert captured[0].foreign == 1
    assert captured[0].total == 1


def test_record_foreign_records_source_ip() -> None:
    captured: list[ForeignClientAgg] = []
    foreign = ForeignClientBuffer(flush=captured.extend, flush_seconds=3600)
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")], trusted_networks=["10.2.10.0/24"], foreign=foreign
    )
    resolver.record_foreign("192.168.1.50")
    resolver.record_foreign("192.168.1.50")
    foreign.flush()
    assert len(captured) == 1
    assert captured[0].ip == "192.168.1.50"
    assert captured[0].hits == 2


def test_forward_records_upstream_rtt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[UpstreamRttAgg] = []
    rtt = UpstreamRttBuffer(flush=captured.extend, flush_seconds=3600)
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
        upstreams=[UpstreamServer("1.1.1.1", 53, "udp", 100)],
        rtt=rtt,
    )
    reply_bytes = DNSRecord.question("example.com", "A").reply().pack()
    monkeypatch.setattr(DNSRecord, "send", lambda self, *a, **k: reply_bytes)
    resolver.handle(DNSRecord.question("example.com", "A"), "10.0.0.1")
    rtt.flush()
    assert len(captured) == 1
    assert captured[0].address == "1.1.1.1"
    assert captured[0].samples == 1


def test_handle_records_query_event() -> None:
    captured: list[QueryEvent] = []
    events = QueryEventBuffer(flush=captured.extend, flush_seconds=3600)
    resolver = _make_resolver(
        [PolicyRule("*", "ads.example", "override", "0.0.0.0", 60)], events=events
    )
    resolver.handle(DNSRecord.question("ads.example", "A"), "10.0.0.1")
    events.flush()
    assert len(captured) == 1
    assert captured[0].client == "10.0.0.1"
    assert captured[0].domain == "ads.example"
    assert captured[0].qtype == "A"
    assert captured[0].rcode == "NOERROR"
    assert captured[0].response == "0.0.0.0"


def test_handle_records_query_response_in_log() -> None:
    captured: list[QueryAgg] = []
    buffer = QueryBuffer(flush=captured.extend, flush_seconds=5)
    resolver = _make_resolver(
        [PolicyRule("*", "ads.example", "override", "0.0.0.0", 60)], buffer=buffer
    )
    resolver.handle(DNSRecord.question("ads.example", "A"), "10.0.0.1")
    buffer.flush()
    assert len(captured) == 1
    assert captured[0].domain == "ads.example"
    assert captured[0].last_action == "override"
    assert captured[0].last_response == "0.0.0.0"


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


def test_blocklist_applies_to_plain_forward_client() -> None:
    # Blocklist is global: a plain forwarding client is blocked with no blocklist action.
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
        blocklist=frozenset({"ads.example"}),
    )
    reply = resolver.handle(DNSRecord.question("ads.example", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.NXDOMAIN


def test_excluded_client_bypasses_blocklist() -> None:
    # excluded_from_blocklist -> the blocked name is not denied; it forwards (SERVFAIL w/o upstreams).
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
        blocklist=frozenset({"ads.example"}),
        blocklist_excluded=frozenset({"1.2.3.4"}),
    )
    reply = resolver.handle(DNSRecord.question("ads.example", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.SERVFAIL


def test_aaaa_returns_nodata_when_ipv6_disabled() -> None:
    # ipv6 disabled -> every AAAA query is answered NOERROR with no records (NODATA).
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(ipv6_enabled=False),
    )
    reply = resolver.handle(DNSRecord.question("example.com", "AAAA"), "1.2.3.4")
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 0


def test_a_query_unaffected_when_ipv6_disabled() -> None:
    # Only AAAA is short-circuited; A still forwards (SERVFAIL without upstreams).
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(ipv6_enabled=False, cache_enabled=False),
    )
    reply = resolver.handle(DNSRecord.question("example.com", "A"), "1.2.3.4")
    assert reply.header.rcode == RCODE.SERVFAIL


def test_aaaa_forwarded_when_ipv6_enabled() -> None:
    # Default (ipv6 enabled) -> AAAA is not short-circuited; it forwards (SERVFAIL w/o upstreams).
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
    )
    reply = resolver.handle(DNSRecord.question("example.com", "AAAA"), "1.2.3.4")
    assert reply.header.rcode == RCODE.SERVFAIL


def test_truncated_udp_response_retries_over_tcp() -> None:
    # UDP answer with TC=1 -> re-send the same upstream over TCP and use that full reply.
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
        upstreams=[UpstreamServer("9.9.9.9", 53, "udp", 0)],
    )
    request = DNSRecord.question("example.com", "A")
    truncated = request.reply()
    truncated.header.tc = 1
    full = request.reply()
    full.add_answer(RR(request.q.qname, QTYPE.A, ttl=60, rdata=A("1.2.3.4")))

    sent_tcp: list[bool] = []

    def fake_send(address: str, port: int, tcp: bool = False, timeout: float | None = None) -> bytes:
        sent_tcp.append(tcp)
        return (full if tcp else truncated).pack()

    request.send = fake_send  # type: ignore[method-assign]
    reply = resolver.handle(request, "1.2.3.4")

    assert sent_tcp == [False, True]
    assert reply.header.rcode == RCODE.NOERROR
    assert [str(rr.rdata) for rr in reply.rr] == ["1.2.3.4"]


def test_untruncated_udp_response_is_not_retried() -> None:
    # A complete UDP answer (TC=0) is used as-is; no TCP re-send.
    resolver = _make_resolver(
        [PolicyRule("*", "*", "forward")],
        RuntimeSettings(cache_enabled=False),
        upstreams=[UpstreamServer("9.9.9.9", 53, "udp", 0)],
    )
    request = DNSRecord.question("example.com", "A")
    answer = request.reply()
    answer.add_answer(RR(request.q.qname, QTYPE.A, ttl=60, rdata=A("1.2.3.4")))

    sent_tcp: list[bool] = []

    def fake_send(address: str, port: int, tcp: bool = False, timeout: float | None = None) -> bytes:
        sent_tcp.append(tcp)
        return answer.pack()

    request.send = fake_send  # type: ignore[method-assign]
    reply = resolver.handle(request, "1.2.3.4")

    assert sent_tcp == [False]
    assert [str(rr.rdata) for rr in reply.rr] == ["1.2.3.4"]


def test_query_buffer_aggregates_counts() -> None:
    captured: list[QueryAgg] = []
    buffer = QueryBuffer(flush=captured.extend, flush_seconds=5)
    buffer.add("1.2.3.4", "example.com", "A", "deny", "")
    buffer.add("1.2.3.4", "example.com", "A", "deny", "")
    buffer.add("1.2.3.4", "other.com", "AAAA", "forward", "2606:4700:4700::1111")
    buffer.flush()
    by_key = {(row.client, row.domain, row.qtype): row for row in captured}
    assert by_key[("1.2.3.4", "example.com", "A")].count == 2
    assert by_key[("1.2.3.4", "other.com", "AAAA")].count == 1
    assert by_key[("1.2.3.4", "other.com", "AAAA")].last_response == "2606:4700:4700::1111"
