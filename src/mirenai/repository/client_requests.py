"""Persistence for per-client DNS request objects (query names).

Aggregated in memory by the DNS server and flushed here in an hourly batch. Each
flush upserts on ``(client, domain, qtype)``: on conflict the stored ``hits`` is
incremented and ``last_seen`` refreshed (``first_seen`` is set once on insert).
"""

from __future__ import annotations

from collections.abc import Collection
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.clientrequests import ClientRequestAgg
from mirenai.repository.models import ClientNewDomainStat, ClientRequest, QueryLog

# How many one-hour intervals the new-domain view covers.
NEW_DOMAIN_WINDOW_HOURS = 20


def _to_dict(row: ClientRequest) -> dict[str, Any]:
    return {
        "id": row.id,
        "client": row.client,
        "domain": row.domain,
        "qtype": row.qtype,
        "hits": row.hits,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
    }


def record_client_requests(rows: list[ClientRequestAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(ClientRequest).values(
                client=row.client,
                domain=row.domain,
                qtype=row.qtype,
                hits=row.hits,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["client", "domain", "qtype"],
                set_={
                    "hits": ClientRequest.hits + stmt.excluded.hits,
                    "last_seen": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)


def list_client_requests(
    limit: int | None = None, offset: int = 0, client: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(ClientRequest)
    if client is not None:
        stmt = stmt.where(ClientRequest.client == client)
    stmt = stmt.order_by(ClientRequest.hits.desc(), ClientRequest.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_client_requests(client: str | None = None) -> int:
    stmt = select(ClientRequest.id)
    if client is not None:
        stmt = stmt.where(ClientRequest.client == client)
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def materialize_new_domains() -> None:
    """Rebuild the dense per-``(hour, client)`` new-domain series from ``client_requests``.

    Gap-fills at write time: for every client ever seen, writes a row for every
    hour in the last ``NEW_DOMAIN_WINDOW_HOURS`` (zero when the client saw no new
    domain that hour), then purges rows outside the window. Recomputing the whole
    window each run makes it idempotent and self-healing (it backfills any hours
    skipped while the worker was down). Run hourly by the DNS worker.
    """
    now = datetime.now()
    current_hour = now.replace(minute=0, second=0, microsecond=0)
    window_start = current_hour - timedelta(hours=NEW_DOMAIN_WINDOW_HOURS - 1)
    with session_scope() as session:
        # Earliest first_seen per (client, domain) dates a domain by when the client first saw it.
        rows = session.execute(
            select(
                ClientRequest.client,
                ClientRequest.domain,
                func.min(ClientRequest.first_seen),
            ).group_by(ClientRequest.client, ClientRequest.domain)
        ).all()
        known_clients: set[str] = set()
        counts: dict[tuple[datetime, str], int] = {}
        for client, _domain, first_seen in rows:
            known_clients.add(client)
            if first_seen is None:
                continue
            bucket = first_seen.replace(minute=0, second=0, microsecond=0)
            if window_start <= bucket <= current_hour:
                counts[(bucket, client)] = counts.get((bucket, client), 0) + 1
        hours = [window_start + timedelta(hours=i) for i in range(NEW_DOMAIN_WINDOW_HOURS)]
        for hour in hours:
            for client in known_clients:
                stmt = sqlite_insert(ClientNewDomainStat).values(
                    hour_start=hour, client=client, new_domains=counts.get((hour, client), 0)
                )
                stmt = stmt.on_conflict_do_update(
                    index_elements=["hour_start", "client"],
                    set_={"new_domains": stmt.excluded.new_domains},
                )
                session.execute(stmt)
        session.execute(
            delete(ClientNewDomainStat).where(ClientNewDomainStat.hour_start < window_start)
        )


def list_new_domain_counts(client: str | None = None) -> list[dict[str, Any]]:
    """Read the dense per-``(hour, client)`` new-domain series (newest hour first).

    A plain read of the materialized table (see :func:`materialize_new_domains`);
    every known client has a row for every hour in the window, zeros included.
    Without ``client`` every client is returned; otherwise just that client's rows.
    """
    stmt = select(ClientNewDomainStat).order_by(
        ClientNewDomainStat.hour_start.desc(), ClientNewDomainStat.client
    )
    if client is not None:
        stmt = stmt.where(ClientNewDomainStat.client == client)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [
            {
                "hour_start": row.hour_start.isoformat(),
                "client": row.client,
                "new_domains": row.new_domains,
            }
            for row in rows
        ]


def list_recent_new_domains(
    limit: int = 100, exclude_clients: Collection[str] | None = None
) -> list[dict[str, Any]]:
    """List the most recently first-seen ``(client, domain)`` pairs, newest first.

    One row per ``(client, domain)`` dated by the earliest ``first_seen`` across
    query types, ordered by that timestamp descending and capped at ``limit``
    (the top-N most recently discovered domains). ``last_action`` is the action
    the query log last recorded for that ``(client, domain)`` (newest
    ``last_seen`` across query types), or ``None`` when the query log has no row.
    Clients in ``exclude_clients`` are filtered out before the limit is applied
    (used to drop clients opted out of new-domain monitoring).
    """
    first_seen = func.min(ClientRequest.first_seen)
    last_action = (
        select(QueryLog.last_action)
        .where(
            QueryLog.client == ClientRequest.client,
            QueryLog.domain == ClientRequest.domain,
        )
        .order_by(QueryLog.last_seen.desc(), QueryLog.id.desc())
        .limit(1)
        .scalar_subquery()
    )
    stmt = select(
        ClientRequest.client,
        ClientRequest.domain,
        first_seen.label("first_seen"),
        last_action.label("last_action"),
    )
    if exclude_clients:
        stmt = stmt.where(ClientRequest.client.not_in(exclude_clients))
    stmt = (
        stmt.group_by(ClientRequest.client, ClientRequest.domain)
        .order_by(first_seen.desc())
        .limit(limit)
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
        return [
            {
                "client": row.client,
                "domain": row.domain,
                "first_seen": row.first_seen.isoformat(),
                "last_action": row.last_action,
            }
            for row in rows
        ]
