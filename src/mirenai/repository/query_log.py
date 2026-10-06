"""Persistence for the per-client query log.

Rows are aggregated in memory by the DNS server and flushed here in batches.
Each flush upserts on ``(client, domain, qtype)``: on conflict the stored count
is incremented and ``last_seen`` / ``last_action`` are refreshed.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import ColumnElement, CursorResult, delete, func, or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.querybuffer import QueryAgg
from mirenai.repository.models import QueryLog

# SQLite caps host parameters per statement; chunk domain IN() lookups to stay under it.
_DOMAIN_CHUNK = 500


def _to_dict(row: QueryLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "client": row.client,
        "domain": row.domain,
        "qtype": row.qtype,
        "count": row.count,
        "last_action": row.last_action,
        "last_response": row.last_response,
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
                last_response=row.last_response,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["client", "domain", "qtype"],
                set_={
                    "count": QueryLog.count + stmt.excluded.count,
                    "last_action": stmt.excluded.last_action,
                    "last_response": stmt.excluded.last_response,
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


def _filters(search: str | None, client: str | None) -> list[ColumnElement[bool]]:
    conditions: list[ColumnElement[bool]] = []
    if client:
        conditions.append(QueryLog.client == client)
    search_condition = _search_filter(search)
    if search_condition is not None:
        conditions.append(search_condition)
    return conditions


def list_queries(
    limit: int | None = None,
    offset: int = 0,
    search: str | None = None,
    client: str | None = None,
) -> list[dict[str, Any]]:
    stmt = select(QueryLog).order_by(QueryLog.last_seen.desc(), QueryLog.id)
    for condition in _filters(search, client):
        stmt = stmt.where(condition)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_queries(search: str | None = None, client: str | None = None) -> int:
    stmt = select(QueryLog.id)
    for condition in _filters(search, client):
        stmt = stmt.where(condition)
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def reset_queries() -> int:
    with session_scope() as session:
        # Session.execute(DELETE) is typed Result but returns CursorResult at runtime.
        result = cast(CursorResult[Any], session.execute(delete(QueryLog)))
        return result.rowcount or 0


def aggregate_domain_totals() -> list[dict[str, Any]]:
    """Aggregate the query log by domain, most-queried first.

    One row per domain: the summed ``count`` across every client and query type,
    the earliest ``first_seen`` and the latest ``last_seen``.
    """
    total = func.sum(QueryLog.count)
    stmt = (
        select(
            QueryLog.domain,
            total.label("count"),
            func.min(QueryLog.first_seen).label("first_seen"),
            func.max(QueryLog.last_seen).label("last_seen"),
        )
        .group_by(QueryLog.domain)
        .order_by(total.desc(), QueryLog.domain)
    )
    with session_scope() as session:
        rows = session.execute(stmt).all()
        return [
            {
                "domain": domain,
                "count": int(count or 0),
                "first_seen": first_seen.isoformat(),
                "last_seen": last_seen.isoformat(),
            }
            for domain, count, first_seen, last_seen in rows
        ]


def clients_for_domains(domains: Sequence[str]) -> dict[str, list[dict[str, Any]]]:
    """Per-domain client breakdown for ``domains``, most-active client first.

    Returns a mapping from each domain to a list of ``{client, count}`` with the
    summed query ``count`` per client across query types. Domains with no rows in
    the query log are omitted.
    """
    if not domains:
        return {}
    total = func.sum(QueryLog.count)
    ordered = list(domains)
    result: dict[str, list[dict[str, Any]]] = {}
    with session_scope() as session:
        for start in range(0, len(ordered), _DOMAIN_CHUNK):
            chunk = ordered[start : start + _DOMAIN_CHUNK]
            stmt = (
                select(QueryLog.domain, QueryLog.client, total.label("count"))
                .where(QueryLog.domain.in_(chunk))
                .group_by(QueryLog.domain, QueryLog.client)
                .order_by(QueryLog.domain, total.desc(), QueryLog.client)
            )
            for domain, client, count in session.execute(stmt).all():
                result.setdefault(domain, []).append(
                    {"client": client, "count": int(count or 0)}
                )
    return result
