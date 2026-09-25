"""Persistence for per-client DNS request objects (query names).

Aggregated in memory by the DNS server and flushed here in an hourly batch. Each
flush upserts on ``(client, domain, qtype)``: on conflict the stored ``hits`` is
incremented and ``last_seen`` refreshed (``first_seen`` is set once on insert).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.clientrequests import ClientRequestAgg
from mirenai.repository.models import ClientRequest


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
