"""SQLAlchemy ORM models.

Tables are registered on ``Base.metadata`` (see ``mirenai.db``) and created at
startup with ``create_all``. Timestamps default to ``datetime('now', 'localtime')``
so SQLite records them in the container's local time zone (``TZ``).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from mirenai.db import Base, BlocklistBase, HostsBase

# datetime('now','localtime') resolves against the container's TZ env var.
_LOCAL_NOW = text("(datetime('now', 'localtime'))")


class Policy(Base):
    __tablename__ = "policies"
    __table_args__ = (UniqueConstraint("client", "domain", name="uq_policies_client_domain"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(255), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    override_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    override_ttl: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class Upstream(Base):
    __tablename__ = "upstreams"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    address: Mapped[str] = mapped_column(String(64), nullable=False)
    port: Mapped[int] = mapped_column(Integer, nullable=False, default=53)
    protocol: Mapped[str] = mapped_column(String(8), nullable=False, default="udp")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class TrustedNetwork(Base):
    """A source subnet (CIDR) permitted to query the resolver.

    When any rows exist the DNS server answers only clients whose address falls
    inside one of them and silently drops everything else; an empty table trusts
    all clients.
    """

    __tablename__ = "trusted_networks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    cidr: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class QueryLog(Base):
    __tablename__ = "query_log"
    __table_args__ = (
        UniqueConstraint("client", "domain", "qtype", name="uq_query_log_client_domain_qtype"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False)
    count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    last_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    last_response: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )


class ClientQueryEvent(Base):
    """Individual DNS query/response events for a client, kept for a short window.

    Unlike :class:`QueryLog` (aggregated counts), this stores one row per query with
    the answer returned, so the API can replay a client's most recent lookups. Rows
    are written in batches by the DNS worker and pruned to a short retention window
    (see ``mirenai.repository.query_events.RETENTION_SECONDS``).
    """

    __tablename__ = "client_query_events"
    __table_args__ = (
        Index("ix_client_query_events_client_created", "client", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False)
    rcode: Mapped[str] = mapped_column(String(16), nullable=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True, server_default=_LOCAL_NOW
    )


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class RuntimeStat(Base):
    """Point-in-time gauges sampled by the DNS server (cache size, blocklist size, ...).

    A small key/value snapshot flushed on the query-flush interval; ``updated_at``
    doubles as the DNS worker's heartbeat.
    """

    __tablename__ = "runtime_stats"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class DnsCacheEntry(Base):
    """A point-in-time snapshot row of one in-memory DNS cache entry.

    The DNS worker periodically mirrors its answer cache into this table because
    the API runs in a separate process and can't read the cache directly. One row
    per cached ``(domain, qtype, qclass)`` answer; the whole table is replaced on
    each snapshot, so rows are at most one snapshot interval stale.
    """

    __tablename__ = "dns_cache_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False)
    qclass: Mapped[str] = mapped_column(String(16), nullable=False)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    answers: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ttl: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )


class UncacheableResponse(Base):
    """A forwarded answer the resolver could not cache, with a reason and hit count.

    One row per ``(client, domain, qtype, reason)``: the DNS worker aggregates these
    in memory and writes them in a batch (upsert ``hits += n``, ``last_seen`` and
    ``last_ttl`` refreshed), attributing each miss to the requesting ``client``. It
    explains *why* an answer wasn't cached — negative (``nxdomain`` / ``error``),
    ``nodata``, ``zero-ttl``, or ``upstream-failure`` — so a low cache-hit rate can be
    diagnosed. ``last_ttl`` is the most recent upstream TTL observed (NULL when the
    response carried no answer records).
    """

    __tablename__ = "uncacheable_responses"
    __table_args__ = (
        UniqueConstraint(
            "client", "domain", "qtype", "reason", name="uq_uncacheable_client_domain_qtype_reason"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    last_ttl: Mapped[int | None] = mapped_column(Integer, nullable=True)
    hits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )


class CacheOutcomeHourly(Base):
    """Hourly count of forwarded-query cache outcomes, one row per (hour, reason).

    A long-format time series for charting *why* forwarded answers were or weren't
    cached — ``cached`` (stored) vs ``nxdomain`` / ``error`` / ``nodata`` /
    ``zero-ttl`` / ``upstream-failure``. Written from an in-memory buffer (upsert on
    ``hour_start`` + ``reason``); buckets older than the retention window are purged
    on write. Independent of the per-client ``forwarded`` tally, which is unchanged.
    """

    __tablename__ = "cache_outcome_hourly"
    __table_args__ = (
        UniqueConstraint("hour_start", "reason", name="uq_cache_outcome_hour_reason"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    hits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class UpstreamHourlyRtt(Base):
    """Per-upstream forward round-trip time for one wall-clock hour.

    Stores the sum and count so the hourly upsert stays exact as flush batches
    accumulate; ``avg_ms`` is derived (``total_ms / samples``) on read.
    """

    __tablename__ = "upstream_hourly_rtt"
    __table_args__ = (
        UniqueConstraint("hour_start", "address", name="uq_upstream_rtt_hour_addr"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    samples: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


class ClientHourlyStat(Base):
    """Per-client DNS query counts for one wall-clock hour, broken down by result.

    Written once an hour from an in-memory buffer (upsert on ``hour_start`` +
    ``client``); buckets older than the retention window are purged on write.
    """

    __tablename__ = "client_hourly_stats"
    __table_args__ = (
        UniqueConstraint("hour_start", "client", name="uq_client_hourly_stats_hour_client"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    total: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    forwarded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    cached: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    overridden: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    local: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )
    denied: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    blocked: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    servfail: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    foreign: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default=text("0")
    )


class ClientRequest(Base):
    """Per-client DNS request object (query name) with a hit counter.

    One row per ``(client, domain, qtype)``. Aggregated in memory and written in
    an hourly batch (upsert: ``hits += n``, ``last_seen`` refreshed).
    """

    __tablename__ = "client_requests"
    __table_args__ = (
        UniqueConstraint("client", "domain", "qtype", name="uq_client_requests_client_domain_qtype"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    qtype: Mapped[str] = mapped_column(String(16), nullable=False)
    hits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )


class ClientNewDomainStat(Base):
    """Per-client count of newly-seen domains in one wall-clock hour.

    Materialized hourly from ``client_requests`` as a *dense* series: every known
    client gets a row for every hour in the window (zero when it saw no new domain
    that hour), so the read endpoint needs no gap-filling.
    """

    __tablename__ = "client_new_domain_stats"
    __table_args__ = (
        UniqueConstraint("hour_start", "client", name="uq_client_new_domain_hour_client"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hour_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    client: Mapped[str] = mapped_column(String(64), nullable=False)
    new_domains: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class ForeignClient(Base):
    """A source IP whose queries were dropped for being outside the trusted subnets.

    One row per untrusted source IP: ``hits`` accumulates across dropped queries,
    ``first_seen`` is set once on insert, and ``last_seen`` is refreshed on every
    flush. The repository caps the table to the most recently seen IPs so a flood
    of spoofed sources can't grow it without bound.
    """

    __tablename__ = "foreign_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    hits: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, index=True
    )


class Blocklist(Base):
    """Configuration for a downloadable DNS blocklist (name, source URL, cadence).

    The list contents themselves live in the separate blocklist database
    (:class:`BlocklistDomain`); this row only tracks how and when to fetch them.
    """

    __tablename__ = "blocklists"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    update_interval_hours: Mapped[int] = mapped_column(Integer, nullable=False, default=24)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    domain_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class BlocklistOverride(Base):
    """A domain exempted from every blocklist (an allowlist entry).

    Override domains are stripped from each blocklist's parsed domains before they
    are written to the blocklist database, so the exemption is applied when lists
    are downloaded rather than on every DNS query.
    """

    __tablename__ = "blocklist_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class LocalZone(Base):
    """Configuration for a downloadable file of locally-served DNS records.

    Each zone is an http(s) source (typically a raw GitHub URL) of ``value,name``
    lines. The downloader fetches it on its cadence, parses it, and stores the
    expanded records (:class:`LocalDnsRecord`); the DNS server then answers them
    authoritatively. This row only tracks how and when to fetch.
    """

    __tablename__ = "local_zones"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    update_interval_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=86400)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_downloaded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_status: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW, onupdate=_LOCAL_NOW
    )


class LocalDnsRecord(Base):
    """One expanded DNS record produced from a :class:`LocalZone`'s source file.

    A ``value,name`` source line expands into a forward record (``A``/``AAAA`` or
    ``CNAME``) and, for addresses, a reverse ``PTR`` record. Records are replaced
    wholesale on each download and loaded into the DNS worker's memory, where they
    are served authoritatively.
    """

    __tablename__ = "local_dns_records"
    __table_args__ = (
        UniqueConstraint(
            "zone_id", "name", "rtype", "value", name="uq_local_dns_records_zone_name_type_value"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    zone_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    rtype: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    ttl: Mapped[int] = mapped_column(Integer, nullable=False, default=300)


class BlocklistDomain(BlocklistBase):
    """A single blocked domain, stored in the separate blocklist database.

    ``blocklist_id`` references :class:`Blocklist` in the configuration database;
    the two live in different files, so this is a logical (not enforced) foreign key.
    """

    __tablename__ = "blocklist_domains"
    __table_args__ = (
        UniqueConstraint("blocklist_id", "domain", name="uq_blocklist_domain"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    blocklist_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    domain: Mapped[str] = mapped_column(String(255), nullable=False, index=True)


class Host(HostsBase):
    """A client host seen by the DNS server, stored in the separate localhosts database.

    One row per source IP: ``query_count`` accumulates across queries, ``first_seen``
    is set once on insert, and ``last_seen`` is refreshed on every flush.
    ``excluded_from_blocklist`` exempts the client from blocklist filtering when set.
    ``flag_new_domains`` (on by default) controls whether this client's newly-seen
    domains surface in the recent-new-domains feed.
    """

    __tablename__ = "hosts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    device_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    icon: Mapped[str | None] = mapped_column(String(255), nullable=True)
    mac_address: Mapped[str | None] = mapped_column(String(64), nullable=True)
    excluded_from_blocklist: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("0")
    )
    flag_new_domains: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=text("1")
    )
    query_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    first_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
    last_seen: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=_LOCAL_NOW
    )
