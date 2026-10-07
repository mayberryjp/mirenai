"""Persistence for forwarded answers that could not be cached.

Aggregated in memory by the DNS server and flushed here on a timer. Each flush
upserts on ``(client, domain, qtype, reason)``: on conflict the stored ``hits`` is
incremented and ``last_seen``/``last_ttl`` refreshed (``first_seen`` is set once
on insert). After writing, the table is capped to the ``MAX_UNCACHEABLE`` most
recently seen rows so a flood of unique names can't grow it without bound.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.uncacheable import UncacheableAgg
from mirenai.repository.models import UncacheableResponse

# Cap on retained rows (newest by last_seen are kept).
MAX_UNCACHEABLE = 5000


def _to_dict(row: UncacheableResponse) -> dict[str, Any]:
    return {
        "id": row.id,
        "client": row.client,
        "domain": row.domain,
        "qtype": row.qtype,
        "reason": row.reason,
        "last_ttl": row.last_ttl,
        "hits": row.hits,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
    }


def record_uncacheable(rows: list[UncacheableAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(UncacheableResponse).values(
                client=row.client,
                domain=row.domain,
                qtype=row.qtype,
                reason=row.reason,
                last_ttl=row.last_ttl,
                hits=row.hits,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["client", "domain", "qtype", "reason"],
                set_={
                    "hits": UncacheableResponse.hits + stmt.excluded.hits,
                    "last_ttl": stmt.excluded.last_ttl,
                    "last_seen": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)
        keep = (
            select(UncacheableResponse.id)
            .order_by(UncacheableResponse.last_seen.desc(), UncacheableResponse.id.desc())
            .limit(MAX_UNCACHEABLE)
        )
        session.execute(delete(UncacheableResponse).where(UncacheableResponse.id.not_in(keep)))


def list_uncacheable(
    limit: int | None = None,
    offset: int = 0,
    reason: str | None = None,
    client: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(UncacheableResponse)
    if reason is not None:
        stmt = stmt.where(UncacheableResponse.reason == reason)
    if client is not None:
        stmt = stmt.where(UncacheableResponse.client == client)
    stmt = stmt.order_by(UncacheableResponse.hits.desc(), UncacheableResponse.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_uncacheable(reason: str | None = None, client: str | None = None) -> int:
    stmt = select(UncacheableResponse.id)
    if reason is not None:
        stmt = stmt.where(UncacheableResponse.reason == reason)
    if client is not None:
        stmt = stmt.where(UncacheableResponse.client == client)
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def delete_uncacheable(entry_id: int) -> bool:
    with session_scope() as session:
        row = session.get(UncacheableResponse, entry_id)
        if row is None:
            return False
        session.delete(row)
        return True


def delete_all_uncacheable(reason: str | None = None, client: str | None = None) -> int:
    """Remove retained uncacheable rows (optionally filtered). Returns the number deleted."""
    stmt = delete(UncacheableResponse)
    if reason is not None:
        stmt = stmt.where(UncacheableResponse.reason == reason)
    if client is not None:
        stmt = stmt.where(UncacheableResponse.client == client)
    with session_scope() as session:
        # Session.execute(DELETE) is typed Result but returns CursorResult at runtime.
        result = cast(CursorResult[Any], session.execute(stmt))
        return result.rowcount or 0
