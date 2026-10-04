from collections.abc import Callable
from ipaddress import ip_address

from dnslib import QTYPE, RCODE, DNSRecord

from mirenai.domain.cache import TTLCache
from mirenai.domain.cacheoutcome import CacheOutcomeBuffer
from mirenai.domain.clientrequests import ClientRequestBuffer
from mirenai.domain.clientstats import ClientStatsBuffer
from mirenai.domain.foreignclients import ForeignClientBuffer
from mirenai.domain.localzones import LocalRecord, parse_zone
from mirenai.domain.policy import PolicyRule
from mirenai.domain.querybuffer import QueryBuffer
from mirenai.domain.queryevents import QueryEventBuffer
from mirenai.domain.resolver import DnsResolver
from mirenai.domain.state import RuntimeSettings, RuntimeState
from mirenai.domain.uncacheable import UncacheableBuffer
from mirenai.domain.upstreamstats import UpstreamRttBuffer


def _noop(_rows: object) -> None:
    return None


def _resolver(
    local_records: list[LocalRecord],
    policies: list[PolicyRule] | None = None,
    settings: RuntimeSettings | None = None,
) -> DnsResolver:
    state = RuntimeState(
        load_settings=lambda: settings or RuntimeSettings(),
        load_policies=lambda: policies or [PolicyRule("*", "*", "forward")],
        load_upstreams=lambda: [],
        load_blocklist=lambda: frozenset(),
        load_blocklist_excluded=lambda: frozenset(),
        load_trusted_networks=lambda: [],
        load_local_records=lambda: local_records,
    )
    flush: Callable[[object], None] = _noop
    return DnsResolver(
        state,
        TTLCache(100),
        QueryBuffer(flush=flush, flush_seconds=5),
        ClientStatsBuffer(flush=flush, flush_seconds=3600),
        ClientRequestBuffer(flush=flush, flush_seconds=3600),
        UpstreamRttBuffer(flush=flush, flush_seconds=3600),
        QueryEventBuffer(flush=flush, flush_seconds=3600),
        ForeignClientBuffer(flush=flush, flush_seconds=3600),
        UncacheableBuffer(flush=flush, flush_seconds=3600),
        CacheOutcomeBuffer(flush=flush, flush_seconds=3600),
    )


def test_local_a_record_answered_authoritatively() -> None:
    resolver = _resolver(parse_zone("192.0.2.10,host.example.lan"))
    reply = resolver.handle(DNSRecord.question("host.example.lan", "A"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 1
    assert str(reply.rr[0].rdata) == "192.0.2.10"


def test_local_ptr_reverse_lookup() -> None:
    resolver = _resolver(parse_zone("192.0.2.10,host.example.lan"))
    ptr_name = ip_address("192.0.2.10").reverse_pointer
    reply = resolver.handle(DNSRecord.question(ptr_name, "PTR"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert "host.example.lan" in str(reply.rr[0].rdata)


def test_rfc1918_ptr_in_local_zone_is_answered() -> None:
    # A private reverse name defined by a local zone is answered authoritatively even
    # though drop_private_ptr is on by default.
    resolver = _resolver(parse_zone("10.4.10.4,nas.lan"))
    ptr_name = ip_address("10.4.10.4").reverse_pointer
    reply = resolver.handle(DNSRecord.question(ptr_name, "PTR"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert "nas.lan" in str(reply.rr[0].rdata)


def test_rfc1918_ptr_without_local_record_is_nodata() -> None:
    # Private reverse lookup with no local record -> NODATA (NOERROR, no answers),
    # not forwarded (which would SERVFAIL without upstreams).
    resolver = _resolver(parse_zone("192.0.2.10,host.example.lan"))
    ptr_name = ip_address("10.4.10.4").reverse_pointer
    reply = resolver.handle(DNSRecord.question(ptr_name, "PTR"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 0


def test_local_name_missing_type_is_nodata_not_forwarded() -> None:
    # Only an A record exists; an AAAA query must return authoritative NODATA
    # (NOERROR, no answers) rather than forwarding (which would SERVFAIL here).
    resolver = _resolver(parse_zone("192.0.2.10,host.example.lan"))
    reply = resolver.handle(DNSRecord.question("host.example.lan", "AAAA"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 0


def test_local_cname_is_chased_to_address() -> None:
    zone = "192.0.2.10,host.example.lan\nhost.example.lan,www.example.lan"
    resolver = _resolver(parse_zone(zone))
    reply = resolver.handle(DNSRecord.question("www.example.lan", "A"), "10.0.0.1")
    rtypes = {rr.rtype for rr in reply.rr}
    assert QTYPE.CNAME in rtypes
    assert QTYPE.A in rtypes
    assert any(str(rr.rdata) == "192.0.2.10" for rr in reply.rr if rr.rtype == QTYPE.A)


def test_cname_query_returns_cname_record() -> None:
    resolver = _resolver(parse_zone("host.example.lan,www.example.lan"))
    reply = resolver.handle(DNSRecord.question("www.example.lan", "CNAME"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert "host.example.lan" in str(reply.rr[0].rdata)


def test_non_local_name_falls_through_to_forward() -> None:
    # No upstreams configured, so a forwarded (non-local) name SERVFAILs — proving
    # the resolver did not treat it as locally owned.
    resolver = _resolver(parse_zone("192.0.2.10,host.example.lan"))
    reply = resolver.handle(DNSRecord.question("elsewhere.example.com", "A"), "10.0.0.1")
    assert reply.header.rcode == RCODE.SERVFAIL


def test_deny_policy_takes_precedence_over_local_record() -> None:
    resolver = _resolver(
        parse_zone("192.0.2.10,host.example.lan"),
        policies=[PolicyRule("*", "*", "deny")],
    )
    reply = resolver.handle(DNSRecord.question("host.example.lan", "A"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NXDOMAIN


def test_ipv6_disabled_suppresses_local_aaaa() -> None:
    resolver = _resolver(
        parse_zone("2001:db8::10,v6.example.lan"),
        settings=RuntimeSettings(ipv6_enabled=False),
    )
    reply = resolver.handle(DNSRecord.question("v6.example.lan", "AAAA"), "10.0.0.1")
    assert reply.header.rcode == RCODE.NOERROR
    assert len(reply.rr) == 0
