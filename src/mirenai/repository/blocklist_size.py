"""Persistence for the blocklist-size time series, sampled hourly.

The blocklist downloader records the current total number of enabled-blocklist
domains once per poll, upserting the current wall-clock hour's bucket. Unlike the
additive cache-outcome series this is a **gauge**: the latest sample within an
hour overwrites the earlier one. Buckets older than ``RETENTION_HOURS`` are purged
on write, giving a rolling window for charting blocklist size over time.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.repository.models import BlocklistSizeHourly

RETENTION_HOURS = 500


def _floor_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _to_dict(row: BlocklistSizeHourly) -> dict[str, Any]:
    return {"hour_start": row.hour_start.isoformat(), "domains": row.domains}


def record_blocklist_size(domains: int) -> None:
    """Upsert the current hour's size sample (latest wins), then purge old buckets."""
    hour = _floor_hour(datetime.now())
    with session_scope() as session:
        stmt = sqlite_insert(BlocklistSizeHourly).values(hour_start=hour, domains=domains)
        stmt = stmt.on_conflict_do_update(
            index_elements=["hour_start"], set_={"domains": stmt.excluded.domains}
        )
        session.execute(stmt)
        cutoff = datetime.now() - timedelta(hours=RETENTION_HOURS)
        session.execute(delete(BlocklistSizeHourly).where(BlocklistSizeHourly.hour_start < cutoff))


def list_blocklist_size(
    limit: int | None = None, offset: int = 0, hours: int | None = None
) -> list[dict[str, Any]]:
    stmt = select(BlocklistSizeHourly)
    if hours is not None:
        stmt = stmt.where(BlocklistSizeHourly.hour_start >= datetime.now() - timedelta(hours=hours))
    stmt = stmt.order_by(BlocklistSizeHourly.hour_start.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_blocklist_size(hours: int | None = None) -> int:
    stmt = select(BlocklistSizeHourly.id)
    if hours is not None:
        stmt = stmt.where(BlocklistSizeHourly.hour_start >= datetime.now() - timedelta(hours=hours))
    with session_scope() as session:
        return len(session.scalars(stmt).all())
