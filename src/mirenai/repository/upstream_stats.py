"""Persistence for per-upstream forward round-trip time, bucketed by hour.

Aggregated in memory by the DNS server and flushed here on the query-flush timer.
Each flush upserts on ``(hour_start, address)`` — adding the batch's sample count
and total and keeping the max — then purges buckets older than ``RETENTION_HOURS``.
``avg_ms`` is derived from ``total_ms / samples`` on read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.upstreamstats import UpstreamRttAgg
from mirenai.repository.models import UpstreamHourlyRtt

RETENTION_HOURS = 500


def _to_dict(row: UpstreamHourlyRtt) -> dict[str, Any]:
    return {
        "hour_start": row.hour_start.isoformat(),
        "address": row.address,
        "samples": row.samples,
        "avg_ms": round(row.total_ms / row.samples, 1) if row.samples else None,
        "max_ms": round(row.max_ms, 1) if row.samples else None,
    }


def _zero_row(hour_iso: str, address: str) -> dict[str, Any]:
    return {"hour_start": hour_iso, "address": address, "samples": 0, "avg_ms": None, "max_ms": None}


def _fill_hours(
    rows: list[dict[str, Any]], hours: int, now: datetime, addresses: list[str]
) -> list[dict[str, Any]]:
    """One row per hour per address across the window (newest first), null-filling gaps."""
    by_key = {(row["hour_start"], row["address"]): row for row in rows}
    end = now.replace(minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(hours=min(hours, RETENTION_HOURS))
    filled: list[dict[str, Any]] = []
    bucket = end
    while bucket >= cutoff:
        hour_iso = bucket.isoformat()
        for address in addresses:
            key = (hour_iso, address)
            filled.append(by_key[key] if key in by_key else _zero_row(hour_iso, address))
        bucket -= timedelta(hours=1)
    return filled


def record_upstream_rtt(rows: list[UpstreamRttAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(UpstreamHourlyRtt).values(
                hour_start=row.hour_start,
                address=row.address,
                samples=row.samples,
                total_ms=row.total_ms,
                max_ms=row.max_ms,
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["hour_start", "address"],
                set_={
                    "samples": UpstreamHourlyRtt.samples + stmt.excluded.samples,
                    "total_ms": UpstreamHourlyRtt.total_ms + stmt.excluded.total_ms,
                    "max_ms": func.max(UpstreamHourlyRtt.max_ms, stmt.excluded.max_ms),
                },
            )
            session.execute(stmt)
        cutoff = datetime.now() - timedelta(hours=RETENTION_HOURS)
        session.execute(delete(UpstreamHourlyRtt).where(UpstreamHourlyRtt.hour_start < cutoff))


def list_upstream_rtt(
    limit: int | None = None,
    offset: int = 0,
    address: str | None = None,
    hours: int | None = None,
    fill: bool = False,
) -> list[dict[str, Any]]:
    now = datetime.now()
    stmt = select(UpstreamHourlyRtt)
    if address is not None:
        stmt = stmt.where(UpstreamHourlyRtt.address == address)
    if hours is not None:
        stmt = stmt.where(UpstreamHourlyRtt.hour_start >= now - timedelta(hours=hours))
    stmt = stmt.order_by(UpstreamHourlyRtt.hour_start.desc(), UpstreamHourlyRtt.address)
    if limit is not None and not fill:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        dicts = [_to_dict(row) for row in rows]
    if fill and hours is not None:
        addresses = [address] if address is not None else sorted({r["address"] for r in dicts})
        dicts = _fill_hours(dicts, hours, now, addresses)
    return dicts


def count_upstream_rtt(address: str | None = None, hours: int | None = None) -> int:
    stmt = select(UpstreamHourlyRtt.id)
    if address is not None:
        stmt = stmt.where(UpstreamHourlyRtt.address == address)
    if hours is not None:
        stmt = stmt.where(UpstreamHourlyRtt.hour_start >= datetime.now() - timedelta(hours=hours))
    with session_scope() as session:
        return len(session.scalars(stmt).all())
