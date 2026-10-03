"""Persistence for a snapshot of the DNS worker's in-memory answer cache.

The cache lives in the DNS worker *process*; the API runs separately and cannot
read it directly (see the DB-relay pattern in ``AGENTS.md``). The worker writes a
full snapshot here on a timer, replacing the previous one in a single
transaction, and ``GET /cache`` reads it back. Rows are therefore at most one
snapshot interval stale, and ``remaining_ttl`` is recomputed from ``expires_at``
on read so it stays accurate between snapshots.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, insert, or_, select

from mirenai.db import session_scope
from mirenai.domain.cacheview import CacheEntrySnapshot
from mirenai.repository.models import DnsCacheEntry


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _search_filter(search: str | None) -> ColumnElement[bool] | None:
    if not search:
        return None
    term = f"%{_escape_like(search)}%"
    return or_(
        DnsCacheEntry.domain.ilike(term, escape="\\"),
        DnsCacheEntry.response.ilike(term, escape="\\"),
    )


def _to_dict(row: DnsCacheEntry, now: datetime) -> dict[str, Any]:
    remaining = int((row.expires_at - now).total_seconds())
    return {
        "domain": row.domain,
        "qtype": row.qtype,
        "qclass": row.qclass,
        "response": row.response,
        "answers": row.answers,
        "ttl": row.ttl,
        "remaining_ttl": max(0, remaining),
        "expires_at": row.expires_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def record_cache_entries(entries: list[CacheEntrySnapshot]) -> None:
    """Replace the stored snapshot with ``entries`` (a full mirror of the cache)."""
    with session_scope() as session:
        session.execute(delete(DnsCacheEntry))
        if not entries:
            return
        session.execute(
            insert(DnsCacheEntry),
            [
                {
                    "domain": entry.domain,
                    "qtype": entry.qtype,
                    "qclass": entry.qclass,
                    "response": entry.response,
                    "answers": entry.answers,
                    "ttl": entry.ttl,
                    "expires_at": entry.expires_at,
                }
                for entry in entries
            ],
        )


def list_cache_entries(
    limit: int | None = None, offset: int = 0, search: str | None = None
) -> list[dict[str, Any]]:
    now = datetime.now()
    stmt = select(DnsCacheEntry).order_by(DnsCacheEntry.domain, DnsCacheEntry.qtype)
    condition = _search_filter(search)
    if condition is not None:
        stmt = stmt.where(condition)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row, now) for row in rows]


def count_cache_entries(search: str | None = None) -> int:
    stmt = select(DnsCacheEntry.id)
    condition = _search_filter(search)
    if condition is not None:
        stmt = stmt.where(condition)
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def cache_updated_at() -> str | None:
    """The timestamp of the most recent snapshot, or ``None`` when empty."""
    with session_scope() as session:
        value = session.scalar(select(func.max(DnsCacheEntry.updated_at)))
    return value.isoformat() if value is not None else None
