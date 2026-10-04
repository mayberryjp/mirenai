"""Core DNS screening logic.

Given a decoded request and the client's source address, decide whether to
forward (with caching), return an override answer, or deny with ``NXDOMAIN``,
then build the wire response. All ``dnslib`` interaction is contained here.
"""

from __future__ import annotations

import random
import time
from ipaddress import IPv4Address, IPv4Network, IPv6Address, ip_address
from itertools import groupby

from dnslib import AAAA, CNAME, PTR, QTYPE, RCODE, RR, A, DNSRecord

from mirenai.domain.blocklist import is_blocked
from mirenai.domain.cache import TTLCache
from mirenai.domain.cacheoutcome import (
    OUTCOME_CACHED,
    OUTCOME_ERROR,
    OUTCOME_NODATA,
    OUTCOME_NXDOMAIN,
    OUTCOME_UPSTREAM_FAILURE,
    OUTCOME_ZERO_TTL,
    CacheOutcomeBuffer,
)
from mirenai.domain.clientrequests import ClientRequestBuffer
from mirenai.domain.clientstats import ClientStatsBuffer
from mirenai.domain.foreignclients import ForeignClientBuffer
from mirenai.domain.localzones import (
    RTYPE_A,
    RTYPE_AAAA,
    RTYPE_CNAME,
    RTYPE_PTR,
    LocalRecord,
    LocalRecords,
)
from mirenai.domain.policy import (
    ACTION_DENY,
    ACTION_OVERRIDE,
    PolicyRule,
    normalize_domain,
    select_policy,
)
from mirenai.domain.querybuffer import QueryBuffer
from mirenai.domain.queryevents import QueryEventBuffer
from mirenai.domain.state import RuntimeSettings, RuntimeState, UpstreamServer
from mirenai.domain.uncacheable import UncacheableBuffer
from mirenai.domain.upstreamstats import UpstreamRttBuffer
from mirenai.logging import get_logger

log = get_logger("dns.resolver")


def _qtype_name(value: int) -> str:
    try:
        return str(QTYPE[value])
    except Exception:
        return str(value)


def _rcode_name(value: int) -> str:
    try:
        return str(RCODE[value])
    except Exception:
        return str(value)


def _answer_summary(reply: DNSRecord) -> str:
    """Comma-join the answer section's rdata (empty when there are no answers)."""
    return ", ".join(str(record.rdata) for record in reply.rr)


def _cache_key(qname: str, qtype: int, qclass: int) -> str:
    return f"{normalize_domain(qname)}|{qtype}|{qclass}"


# Guard against a CNAME loop while chasing a local alias chain to its address.
_MAX_CNAME_DEPTH = 8

_RFC1918_NETWORKS = (
    IPv4Network("10.0.0.0/8"),
    IPv4Network("172.16.0.0/12"),
    IPv4Network("192.168.0.0/16"),
)
_INADDR_ARPA_SUFFIX = ".in-addr.arpa"


def _is_rfc1918_ptr(qname: str) -> bool:
    """Whether ``qname`` is the reverse (PTR) name of an RFC1918 private IPv4 address.

    ``4.10.4.10.in-addr.arpa`` -> ``10.4.10.4`` (private, ``True``). Non-reverse names,
    partial reverse zones (not a full /32), and public addresses return ``False``.
    """
    name = qname.rstrip(".")
    if not name.endswith(_INADDR_ARPA_SUFFIX):
        return False
    labels = name[: -len(_INADDR_ARPA_SUFFIX)].split(".")
    if len(labels) != 4:
        return False
    try:
        addr = IPv4Address(".".join(reversed(labels)))
    except ValueError:
        return False
    return any(addr in net for net in _RFC1918_NETWORKS)


class DnsResolver:
    def __init__(
        self,
        state: RuntimeState,
        cache: TTLCache[bytes],
        buffer: QueryBuffer,
        stats: ClientStatsBuffer,
        requests: ClientRequestBuffer,
        rtt: UpstreamRttBuffer,
        events: QueryEventBuffer,
        foreign: ForeignClientBuffer,
        uncached: UncacheableBuffer,
        outcomes: CacheOutcomeBuffer,
    ) -> None:
        self._state = state
        self._cache = cache
        self._buffer = buffer
        self._stats = stats
        self._requests = requests
        self._rtt = rtt
        self._events = events
        self._foreign = foreign
        self._uncached = uncached
        self._outcomes = outcomes

    def is_trusted(self, client_ip: str) -> bool:
        """Whether a query from ``client_ip`` should be answered at all."""
        return self._state.network_filter.is_trusted(client_ip)

    def record_foreign(self, client_ip: str) -> None:
        """Count a query dropped for coming from an untrusted source network."""
        self._stats.add_foreign()
        self._foreign.add(client_ip)
        if self._state.settings.log_queries:
            log.info("dropped query from untrusted network %s", client_ip)

    def handle(self, request: DNSRecord, client_ip: str) -> DNSRecord:
        question = request.q
        qname = normalize_domain(str(question.qname))
        settings = self._state.settings

        if (
            settings.drop_private_ptr
            and question.qtype == QTYPE.PTR
            and _is_rfc1918_ptr(qname)
            and not self._state.local_records.owns(qname)
        ):
            # Unmatched RFC1918 reverse lookups: answer NODATA and record nothing so
            # chatty private-range PTR scans never reach the logs, stats, or buffers.
            return self._nodata(request)

        qtype_name = _qtype_name(question.qtype)
        rule = select_policy(self._state.policies, client_ip, str(question.qname))
        action = rule.action if rule is not None else settings.default_action

        if question.qtype == QTYPE.AAAA and not settings.ipv6_enabled:
            reply = self._nodata(request)
            result = "nodata"
        elif action == ACTION_OVERRIDE and rule is not None:
            reply = self._override(request, rule)
            result = "override"
        elif action == ACTION_DENY:
            reply = self._deny(request)
            result = "deny"
        elif client_ip not in self._state.blocklist_excluded and is_blocked(
            self._state.blocklist, qname
        ):
            reply = self._deny(request)
            result = "blocklist"
        else:
            reply, result = self._resolve_local_or_forward(request, settings, client_ip)

        answer = _answer_summary(reply)
        self._buffer.add(client_ip, qname, qtype_name, result, answer)
        self._stats.add(client_ip, result)
        self._requests.add(client_ip, qname, qtype_name)
        self._events.add(
            client_ip, qname, qtype_name, _rcode_name(reply.header.rcode), answer
        )
        if settings.log_queries:
            log.info(
                "query client=%s name=%s type=%s action=%s rcode=%s answers=%d",
                client_ip,
                qname,
                qtype_name,
                result,
                _rcode_name(reply.header.rcode),
                len(reply.rr),
            )
        return reply

    def _deny(self, request: DNSRecord) -> DNSRecord:
        reply = request.reply()
        reply.header.rcode = RCODE.NXDOMAIN
        return reply

    def _nodata(self, request: DNSRecord) -> DNSRecord:
        # NOERROR with no answers (NODATA): the name exists but has no record of this
        # type, so clients fall back to an A lookup instead of treating it as missing.
        return request.reply()

    def _override(self, request: DNSRecord, rule: PolicyRule) -> DNSRecord:
        reply = request.reply()
        question = request.q
        qtype = question.qtype
        ttl = rule.override_ttl
        values = [item.strip() for item in (rule.override_response or "").split(",") if item.strip()]
        added = 0
        for value in values:
            try:
                addr = ip_address(value)
            except ValueError:
                log.warning("invalid override value %r for domain %s", value, rule.domain)
                continue
            if qtype == QTYPE.A and isinstance(addr, IPv4Address):
                reply.add_answer(RR(question.qname, QTYPE.A, ttl=ttl, rdata=A(str(addr))))
                added += 1
            elif qtype == QTYPE.AAAA and isinstance(addr, IPv6Address):
                reply.add_answer(RR(question.qname, QTYPE.AAAA, ttl=ttl, rdata=AAAA(str(addr))))
                added += 1
        if added == 0:
            log.warning(
                "override for %s produced no answers for type %s",
                rule.domain,
                _qtype_name(qtype),
            )
        return reply

    def _resolve_local_or_forward(
        self, request: DNSRecord, settings: RuntimeSettings, client: str
    ) -> tuple[DNSRecord, str]:
        """Answer from local records if the name is served locally, else forward."""
        local = self._local(request)
        if local is not None:
            return local, "local"
        return self._forward_or_cache(request, settings, client)

    def _local(self, request: DNSRecord) -> DNSRecord | None:
        """Build an authoritative answer from local records, or ``None`` if not owned.

        A name present in any local zone is answered here (never forwarded): matching
        records are returned, or an empty NOERROR (NODATA) when the name exists but
        carries no record of the requested type, so a local-only name never leaks to
        an upstream. CNAME aliases are chased to their address within the local set.
        """
        question = request.q
        name = normalize_domain(str(question.qname))
        records = self._state.local_records
        if not records.owns(name):
            return None
        reply = request.reply()
        qtype = question.qtype
        if qtype in (QTYPE.A, QTYPE.AAAA):
            self._add_address_chain(reply, question.qname, name, qtype, records, 0)
        elif qtype == QTYPE.CNAME:
            self._add_records(reply, question.qname, records.get(name, RTYPE_CNAME))
        elif qtype == QTYPE.PTR:
            self._add_records(reply, question.qname, records.get(name, RTYPE_PTR))
        return reply

    def _add_address_chain(
        self,
        reply: DNSRecord,
        owner: object,
        name: str,
        qtype: int,
        records: LocalRecords,
        depth: int,
    ) -> None:
        """Add A/AAAA answers for ``name``, following a local CNAME chain if needed."""
        rtype = RTYPE_A if qtype == QTYPE.A else RTYPE_AAAA
        matches = records.get(name, rtype)
        if matches:
            for record in matches:
                rdata = self._build_addr_rdata(qtype, record.value)
                if rdata is not None:
                    reply.add_answer(RR(owner, qtype, ttl=record.ttl, rdata=rdata))
            return
        if depth >= _MAX_CNAME_DEPTH:
            return
        alias = records.cname(name)
        if alias is None:
            return
        reply.add_answer(RR(owner, QTYPE.CNAME, ttl=alias.ttl, rdata=CNAME(alias.value)))
        if records.owns(alias.value):
            self._add_address_chain(reply, alias.value, alias.value, qtype, records, depth + 1)

    @staticmethod
    def _add_records(reply: DNSRecord, owner: object, records: list[LocalRecord]) -> None:
        """Add plain CNAME/PTR answers for a name."""
        for record in records:
            if record.rtype == RTYPE_CNAME:
                reply.add_answer(RR(owner, QTYPE.CNAME, ttl=record.ttl, rdata=CNAME(record.value)))
            elif record.rtype == RTYPE_PTR:
                reply.add_answer(RR(owner, QTYPE.PTR, ttl=record.ttl, rdata=PTR(record.value)))

    @staticmethod
    def _build_addr_rdata(qtype: int, value: str) -> A | AAAA | None:
        try:
            return A(value) if qtype == QTYPE.A else AAAA(value)
        except Exception:
            log.warning("invalid local %s record value %r", _qtype_name(qtype), value)
            return None

    def _forward_or_cache(
        self, request: DNSRecord, settings: RuntimeSettings, client: str
    ) -> tuple[DNSRecord, str]:
        question = request.q
        name = normalize_domain(str(question.qname))
        qtype = _qtype_name(question.qtype)
        key = _cache_key(str(question.qname), question.qtype, question.qclass)

        if settings.cache_enabled:
            entry = self._cache.get(key)
            if entry is not None:
                reply = DNSRecord.parse(entry.value)
                reply.header.id = request.header.id
                remaining = max(0, int(entry.expires_at - time.monotonic()))
                for record in (*reply.rr, *reply.auth, *reply.ar):
                    record.ttl = remaining
                return reply, "forward-cache"

        reply = self._forward(request, settings)
        if reply is None:
            servfail = request.reply()
            servfail.header.rcode = RCODE.SERVFAIL
            if settings.cache_enabled:
                self._skip_cache(client, name, qtype, OUTCOME_UPSTREAM_FAILURE, None)
            return servfail, "servfail"

        if settings.cache_enabled:
            self._cache_or_record_skip(key, reply, name, qtype, settings, client)
        return reply, "forward"

    def _cache_or_record_skip(
        self,
        key: str,
        reply: DNSRecord,
        name: str,
        qtype: str,
        settings: RuntimeSettings,
        client: str,
    ) -> None:
        """Cache a positive answer, or record why it could not be cached.

        Only called when caching is enabled. Positive answers with a usable TTL are
        stored (counted as the ``cached`` outcome); every other result is counted by
        reason in the uncacheable detail table (attributed to ``client``) and the
        hourly cache-outcome series so a low cache-hit rate can be diagnosed.
        """
        rcode = reply.header.rcode
        if rcode == RCODE.NXDOMAIN:
            self._skip_cache(client, name, qtype, OUTCOME_NXDOMAIN, None)
            return
        if rcode != RCODE.NOERROR:
            self._skip_cache(client, name, qtype, OUTCOME_ERROR, None)
            return
        if not reply.rr:
            self._skip_cache(client, name, qtype, OUTCOME_NODATA, None)
            return
        raw_ttl = min(int(record.ttl) for record in reply.rr)
        ttl = self._compute_ttl(reply, settings)
        if ttl > 0:
            self._cache.set(key, reply.pack(), ttl)
            self._outcomes.add(OUTCOME_CACHED)
            return
        self._skip_cache(client, name, qtype, OUTCOME_ZERO_TTL, raw_ttl)

    def _skip_cache(
        self, client: str, name: str, qtype: str, reason: str, ttl: int | None
    ) -> None:
        """Record a forwarded answer that wasn't cached, by reason (detail + hourly)."""
        self._uncached.add(client, name, qtype, reason, ttl)
        self._outcomes.add(reason)

    def _forward(self, request: DNSRecord, settings: RuntimeSettings) -> DNSRecord | None:
        upstreams = self._state.upstreams
        if not upstreams:
            log.warning("no upstream resolvers configured; cannot forward")
            return None
        for upstream in self._balanced_order(upstreams):
            try:
                start = time.perf_counter()
                use_tcp = upstream.protocol == "tcp"
                raw = request.send(
                    upstream.address,
                    upstream.port,
                    tcp=use_tcp,
                    timeout=settings.forward_timeout,
                )
                reply = DNSRecord.parse(raw)
                if reply.header.tc and not use_tcp:
                    # Truncated over UDP: retry the same upstream over TCP for the full answer.
                    raw = request.send(
                        upstream.address,
                        upstream.port,
                        tcp=True,
                        timeout=settings.forward_timeout,
                    )
                    reply = DNSRecord.parse(raw)
                self._rtt.add(upstream.address, (time.perf_counter() - start) * 1000)
                return reply
            except Exception as exc:
                log.warning(
                    "upstream %s:%s (%s) failed: %s",
                    upstream.address,
                    upstream.port,
                    upstream.protocol,
                    exc,
                )
        log.warning("all upstream resolvers failed")
        return None

    @staticmethod
    def _balanced_order(upstreams: list[UpstreamServer]) -> list[UpstreamServer]:
        """Order by ascending priority, load-balancing equal-priority peers at random."""
        by_priority = sorted(upstreams, key=lambda u: u.priority)
        ordered: list[UpstreamServer] = []
        for _priority, group in groupby(by_priority, key=lambda u: u.priority):
            peers = list(group)
            random.shuffle(peers)  # nosec B311  (load balancing, not security)
            ordered.extend(peers)
        return ordered

    def _compute_ttl(self, reply: DNSRecord, settings: RuntimeSettings) -> int:
        ttl = min(int(record.ttl) for record in reply.rr)
        return max(settings.cache_min_ttl, min(ttl, settings.cache_max_ttl))
