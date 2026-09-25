"""Core DNS screening logic.

Given a decoded request and the client's source address, decide whether to
forward (with caching), return an override answer, or deny with ``NXDOMAIN``,
then build the wire response. All ``dnslib`` interaction is contained here.
"""

from __future__ import annotations

import time
from ipaddress import IPv4Address, IPv6Address, ip_address

from dnslib import AAAA, QTYPE, RCODE, RR, A, DNSRecord

from mirenai.domain.blocklist import is_blocked
from mirenai.domain.cache import TTLCache
from mirenai.domain.policy import (
    ACTION_BLOCKLIST,
    ACTION_DENY,
    ACTION_OVERRIDE,
    PolicyRule,
    normalize_domain,
    select_policy,
)
from mirenai.domain.querybuffer import QueryBuffer
from mirenai.domain.state import RuntimeSettings, RuntimeState
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


def _cache_key(qname: str, qtype: int, qclass: int) -> str:
    return f"{normalize_domain(qname)}|{qtype}|{qclass}"


class DnsResolver:
    def __init__(self, state: RuntimeState, cache: TTLCache[bytes], buffer: QueryBuffer) -> None:
        self._state = state
        self._cache = cache
        self._buffer = buffer

    def handle(self, request: DNSRecord, client_ip: str) -> DNSRecord:
        question = request.q
        qname = normalize_domain(str(question.qname))
        qtype_name = _qtype_name(question.qtype)
        settings = self._state.settings

        rule = select_policy(self._state.policies, client_ip, str(question.qname))
        action = rule.action if rule is not None else settings.default_action

        if action == ACTION_OVERRIDE and rule is not None:
            reply = self._override(request, rule)
            result = "override"
        elif action == ACTION_DENY:
            reply = self._deny(request)
            result = "deny"
        elif action == ACTION_BLOCKLIST and is_blocked(self._state.blocklist, qname):
            reply = self._deny(request)
            result = "blocklist"
        else:
            reply, result = self._forward_or_cache(request, settings)

        self._buffer.add(client_ip, qname, qtype_name, result)
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

    def _forward_or_cache(
        self, request: DNSRecord, settings: RuntimeSettings
    ) -> tuple[DNSRecord, str]:
        question = request.q
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
            return servfail, "servfail"

        if settings.cache_enabled:
            ttl = self._compute_ttl(reply, settings)
            if ttl > 0:
                self._cache.set(key, reply.pack(), ttl)
        return reply, "forward"

    def _forward(self, request: DNSRecord, settings: RuntimeSettings) -> DNSRecord | None:
        upstreams = self._state.upstreams
        if not upstreams:
            log.warning("no upstream resolvers configured; cannot forward")
            return None
        for upstream in upstreams:
            try:
                raw = request.send(
                    upstream.address,
                    upstream.port,
                    tcp=upstream.protocol == "tcp",
                    timeout=settings.forward_timeout,
                )
                return DNSRecord.parse(raw)
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

    def _compute_ttl(self, reply: DNSRecord, settings: RuntimeSettings) -> int:
        answer_ttls = [int(record.ttl) for record in reply.rr]
        if answer_ttls:
            ttl = min(answer_ttls)
        else:
            # Negative response: fall back to the authority section (SOA) TTL.
            auth_ttls = [int(record.ttl) for record in reply.auth]
            ttl = min(auth_ttls) if auth_ttls else settings.cache_min_ttl
        return max(settings.cache_min_ttl, min(ttl, settings.cache_max_ttl))
