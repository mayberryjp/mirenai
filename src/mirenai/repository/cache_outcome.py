"""Persistence for the forwarded-query cache-outcome series, bucketed by hour.

Aggregated in memory by the DNS server and flushed here on the query-flush timer.
Each flush upserts on ``(hour_start, reason)`` — adding the batch's hits — then
purges buckets older than ``RETENTION_HOURS`` (a rolling ~100-hour window). The
read side can zero-fill so every hour has a row for every outcome, ready to graph.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.cacheoutcome import CACHE_OUTCOME_REASONS, CacheOutcomeAgg
from mirenai.repository.models import CacheOutcomeHourly

RETENTION_HOURS = 100


def _to_dict(row: CacheOutcomeHourly) -> dict[str, Any]:
    return {
        "hour_start": row.hour_start.isoformat(),
        "reason": row.reason,
        "hits": row.hits,
    }


def _zero_row(hour_iso: str, reason: str) -> dict[str, Any]:
    return {"hour_start": hour_iso, "reason": reason, "hits": 0}


def _fill_hours(rows: list[dict[str, Any]], hours: int, now: datetime) -> list[dict[str, Any]]:
    """One row per hour per reason across the window (newest first), zero-filling gaps."""
    by_key = {(row["hour_start"], row["reason"]): row for row in rows}
    end = now.replace(minute=0, second=0, microsecond=0)
    cutoff = now - timedelta(hours=min(hours, RETENTION_HOURS))
    filled: list[dict[str, Any]] = []
    bucket = end
    while bucket >= cutoff:
        hour_iso = bucket.isoformat()
        for reason in CACHE_OUTCOME_REASONS:
            key = (hour_iso, reason)
            filled.append(by_key[key] if key in by_key else _zero_row(hour_iso, reason))
        bucket -= timedelta(hours=1)
    return filled


def record_cache_outcomes(rows: list[CacheOutcomeAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(CacheOutcomeHourly).values(
                hour_start=row.hour_start, reason=row.reason, hits=row.hits
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["hour_start", "reason"],
                set_={"hits": CacheOutcomeHourly.hits + stmt.excluded.hits},
            )
            session.execute(stmt)
        cutoff = datetime.now() - timedelta(hours=RETENTION_HOURS)
        session.execute(delete(CacheOutcomeHourly).where(CacheOutcomeHourly.hour_start < cutoff))


def list_cache_outcomes(
    limit: int | None = None, offset: int = 0, hours: int | None = None, fill: bool = False
) -> list[dict[str, Any]]:
    now = datetime.now()
    stmt = select(CacheOutcomeHourly)
    if hours is not None:
        stmt = stmt.where(CacheOutcomeHourly.hour_start >= now - timedelta(hours=hours))
    stmt = stmt.order_by(CacheOutcomeHourly.hour_start.desc(), CacheOutcomeHourly.reason)
    if limit is not None and not fill:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        dicts = [_to_dict(row) for row in rows]
    if fill and hours is not None:
        dicts = _fill_hours(dicts, hours, now)
    return dicts


def count_cache_outcomes(hours: int | None = None) -> int:
    stmt = select(CacheOutcomeHourly.id)
    if hours is not None:
        stmt = stmt.where(CacheOutcomeHourly.hour_start >= datetime.now() - timedelta(hours=hours))
    with session_scope() as session:
        return len(session.scalars(stmt).all())
