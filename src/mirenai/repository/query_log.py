"""Persistence for the per-client query log.

Rows are aggregated in memory by the DNS server and flushed here in batches.
Each flush upserts on ``(client, domain, qtype)``: on conflict the stored count
is incremented and ``last_seen`` / ``last_action`` are refreshed.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import ColumnElement, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.querybuffer import QueryAgg
from mirenai.repository.models import QueryLog


def _to_dict(row: QueryLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "client": row.client,
        "domain": row.domain,
        "qtype": row.qtype,
        "count": row.count,
        "last_action": row.last_action,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
    }


def record_queries(rows: list[QueryAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(QueryLog).values(
                client=row.client,
                domain=row.domain,
                qtype=row.qtype,
                count=row.count,
                last_action=row.last_action,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["client", "domain", "qtype"],
                set_={
                    "count": QueryLog.count + stmt.excluded.count,
                    "last_action": stmt.excluded.last_action,
                    "last_seen": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_filter(search: str | None) -> ColumnElement[bool] | None:
    if not search:
        return None
    term = f"%{_escape_like(search)}%"
    return or_(
        QueryLog.client.ilike(term, escape="\\"),
        QueryLog.domain.ilike(term, escape="\\"),
    )


def list_queries(
    limit: int | None = None, offset: int = 0, search: str | None = None
) -> list[dict[str, Any]]:
    stmt = select(QueryLog).order_by(QueryLog.last_seen.desc(), QueryLog.id)
    condition = _search_filter(search)
    if condition is not None:
        stmt = stmt.where(condition)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_queries(search: str | None = None) -> int:
    stmt = select(QueryLog.id)
    condition = _search_filter(search)
    if condition is not None:
        stmt = stmt.where(condition)
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def reset_queries() -> int:
    with session_scope() as session:
        result = session.execute(delete(QueryLog))
        return result.rowcount or 0
