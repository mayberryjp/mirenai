"""Persistence for per-client hourly query statistics.

Aggregated in memory by the DNS server (bucketed by hour) and flushed here once
an hour. Each flush upserts on ``(hour_start, client)``, adding the batch's
counts, then purges buckets older than ``RETENTION_HOURS``.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.clientstats import ClientStatAgg
from mirenai.repository.models import ClientHourlyStat

RETENTION_HOURS = 500


def _fill_hours(
    rows: list[dict[str, Any]],
    hours: int,
    now: datetime,
    make_zero: Callable[[str], dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return one row per hour across the window (newest first), zero-filling gaps.

    The window is capped at ``RETENTION_HOURS`` since older buckets are purged.
    """
    by_hour = {row["hour_start"]: row for row in rows}
    end = now.replace(minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(hours=min(hours, RETENTION_HOURS))
    filled: list[dict[str, Any]] = []
    bucket = end
    while bucket >= cutoff:
        key = bucket.isoformat()
        filled.append(by_hour[key] if key in by_hour else make_zero(key))
        bucket -= timedelta(hours=1)
    return filled


def _to_dict(row: ClientHourlyStat) -> dict[str, Any]:
    return {
        "id": row.id,
        "hour_start": row.hour_start.isoformat(),
        "client": row.client,
        "total": row.total,
        "forwarded": row.forwarded,
        "cached": row.cached,
        "overridden": row.overridden,
        "denied": row.denied,
        "blocked": row.blocked,
        "servfail": row.servfail,
    }


def _zero_client_row(hour_iso: str, client: str) -> dict[str, Any]:
    return {
        "id": None,
        "hour_start": hour_iso,
        "client": client,
        "total": 0,
        "forwarded": 0,
        "cached": 0,
        "overridden": 0,
        "denied": 0,
        "blocked": 0,
        "servfail": 0,
    }


def record_client_stats(rows: list[ClientStatAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(ClientHourlyStat).values(
                hour_start=row.hour_start,
                client=row.client,
                total=row.total,
                forwarded=row.forwarded,
                cached=row.cached,
                overridden=row.overridden,
                denied=row.denied,
                blocked=row.blocked,
                servfail=row.servfail,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["hour_start", "client"],
                set_={
                    "total": ClientHourlyStat.total + stmt.excluded.total,
                    "forwarded": ClientHourlyStat.forwarded + stmt.excluded.forwarded,
                    "cached": ClientHourlyStat.cached + stmt.excluded.cached,
                    "overridden": ClientHourlyStat.overridden + stmt.excluded.overridden,
                    "denied": ClientHourlyStat.denied + stmt.excluded.denied,
                    "blocked": ClientHourlyStat.blocked + stmt.excluded.blocked,
                    "servfail": ClientHourlyStat.servfail + stmt.excluded.servfail,
                },
            )
            session.execute(stmt)
        cutoff = datetime.now() - timedelta(hours=RETENTION_HOURS)
        session.execute(delete(ClientHourlyStat).where(ClientHourlyStat.hour_start < cutoff))


def list_client_stats(
    limit: int | None = None,
    offset: int = 0,
    client: str | None = None,
    hours: int | None = None,
    fill: bool = False,
) -> list[dict[str, Any]]:
    now = datetime.now()
    stmt = select(ClientHourlyStat)
    if client is not None:
        stmt = stmt.where(ClientHourlyStat.client == client)
    if hours is not None:
        stmt = stmt.where(ClientHourlyStat.hour_start >= now - timedelta(hours=hours))
    stmt = stmt.order_by(ClientHourlyStat.hour_start.desc(), ClientHourlyStat.client)
    if limit is not None and not fill:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        dicts = [_to_dict(row) for row in rows]
    if fill and hours is not None and client is not None:
        client_ip: str = client
        dicts = _fill_hours(dicts, hours, now, lambda key: _zero_client_row(key, client_ip))
    return dicts


def count_client_stats(client: str | None = None, hours: int | None = None) -> int:
    stmt = select(ClientHourlyStat.id)
    if client is not None:
        stmt = stmt.where(ClientHourlyStat.client == client)
    if hours is not None:
        stmt = stmt.where(ClientHourlyStat.hour_start >= datetime.now() - timedelta(hours=hours))
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def _site_to_dict(row: Any) -> dict[str, Any]:
    return {
        "hour_start": row.hour_start.isoformat(),
        "total": int(row.total),
        "forwarded": int(row.forwarded),
        "cached": int(row.cached),
        "overridden": int(row.overridden),
        "denied": int(row.denied),
        "blocked": int(row.blocked),
        "servfail": int(row.servfail),
        "clients": int(row.clients),
    }


def _zero_site_row(hour_iso: str) -> dict[str, Any]:
    return {
        "hour_start": hour_iso,
        "total": 0,
        "forwarded": 0,
        "cached": 0,
        "overridden": 0,
        "denied": 0,
        "blocked": 0,
        "servfail": 0,
        "clients": 0,
    }


def list_site_hourly_stats(
    limit: int | None = None, offset: int = 0, hours: int | None = None, fill: bool = False
) -> list[dict[str, Any]]:
    """Hourly totals summed across all clients (one row per hour)."""
    now = datetime.now()
    stmt = select(
        ClientHourlyStat.hour_start,
        func.sum(ClientHourlyStat.total).label("total"),
        func.sum(ClientHourlyStat.forwarded).label("forwarded"),
        func.sum(ClientHourlyStat.cached).label("cached"),
        func.sum(ClientHourlyStat.overridden).label("overridden"),
        func.sum(ClientHourlyStat.denied).label("denied"),
        func.sum(ClientHourlyStat.blocked).label("blocked"),
        func.sum(ClientHourlyStat.servfail).label("servfail"),
        func.count(func.distinct(ClientHourlyStat.client)).label("clients"),
    )
    if hours is not None:
        stmt = stmt.where(ClientHourlyStat.hour_start >= now - timedelta(hours=hours))
    stmt = stmt.group_by(ClientHourlyStat.hour_start).order_by(ClientHourlyStat.hour_start.desc())
    if limit is not None and not fill:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.execute(stmt).all()
        dicts = [_site_to_dict(row) for row in rows]
    if fill and hours is not None:
        dicts = _fill_hours(dicts, hours, now, _zero_site_row)
    return dicts


def count_site_hourly_stats(hours: int | None = None) -> int:
    stmt = select(func.count(func.distinct(ClientHourlyStat.hour_start)))
    if hours is not None:
        stmt = stmt.where(ClientHourlyStat.hour_start >= datetime.now() - timedelta(hours=hours))
    with session_scope() as session:
        return int(session.scalar(stmt) or 0)
