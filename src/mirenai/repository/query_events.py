"""Persistence for individual per-client DNS query/response events.

Written in batches by the DNS worker (one row per query) and pruned to a short
retention window on every flush, so the table stays small and the API can return
a client's most recent lookups. Read by ``GET /clients/<ip>/queries``.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, insert, select

from mirenai.db import session_scope
from mirenai.domain.queryevents import QueryEvent
from mirenai.repository.models import ClientQueryEvent

# How long individual query events are retained before being pruned.
RETENTION_SECONDS = 3600


def _to_dict(row: ClientQueryEvent) -> dict[str, Any]:
    return {
        "timestamp": row.created_at.isoformat(),
        "client": row.client,
        "domain": row.domain,
        "qtype": row.qtype,
        "rcode": row.rcode,
        "response": row.response,
    }


def record_query_events(rows: list[QueryEvent]) -> None:
    if not rows:
        return
    values = [
        {
            "client": row.client,
            "domain": row.domain,
            "qtype": row.qtype,
            "rcode": row.rcode,
            "response": row.response,
            "created_at": row.created_at,
        }
        for row in rows
    ]
    cutoff = datetime.now() - timedelta(seconds=RETENTION_SECONDS)
    with session_scope() as session:
        session.execute(insert(ClientQueryEvent), values)
        session.execute(delete(ClientQueryEvent).where(ClientQueryEvent.created_at < cutoff))


def list_recent_queries(
    client: str, seconds: int, limit: int | None = None
) -> list[dict[str, Any]]:
    """Return a client's query events from the last ``seconds``, newest first."""
    cutoff = datetime.now() - timedelta(seconds=seconds)
    stmt = (
        select(ClientQueryEvent)
        .where(ClientQueryEvent.client == client, ClientQueryEvent.created_at >= cutoff)
        .order_by(ClientQueryEvent.created_at.desc(), ClientQueryEvent.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]
