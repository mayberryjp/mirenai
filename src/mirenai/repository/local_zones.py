"""Persistence for local DNS zones and their expanded records.

Both the zone *configuration* (name, URL, cadence, status) and the expanded
*records* live in the main configuration database — local zones are small
operator-managed data, unlike the multi-million-entry blocklists that get their
own file. The DNS worker loads every enabled zone's records into memory and
serves them authoritatively (see :mod:`mirenai.domain.localzones`).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, delete, func, insert, select

from mirenai.db import session_scope
from mirenai.domain.localzones import LocalRecord
from mirenai.repository.models import LocalDnsRecord, LocalZone


def _to_dict(zone: LocalZone) -> dict[str, Any]:
    return {
        "id": zone.id,
        "name": zone.name,
        "url": zone.url,
        "update_interval_seconds": zone.update_interval_seconds,
        "enabled": zone.enabled,
        "record_count": zone.record_count,
        "last_downloaded_at": zone.last_downloaded_at.isoformat()
        if zone.last_downloaded_at is not None
        else None,
        "last_status": zone.last_status,
        "created_at": zone.created_at.isoformat(),
        "updated_at": zone.updated_at.isoformat(),
    }


def list_local_zones(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(LocalZone).order_by(LocalZone.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_local_zones() -> int:
    with session_scope() as session:
        return len(session.scalars(select(LocalZone.id)).all())


def get_local_zone(zone_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        zone = session.get(LocalZone, zone_id)
        return _to_dict(zone) if zone is not None else None


def create_local_zone(data: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        zone = LocalZone(
            name=data["name"],
            url=data["url"],
            update_interval_seconds=data.get("update_interval_seconds", 86400),
            enabled=data.get("enabled", True),
        )
        session.add(zone)
        session.flush()
        return _to_dict(zone)


def update_local_zone(zone_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    with session_scope() as session:
        zone = session.get(LocalZone, zone_id)
        if zone is None:
            return None
        for field in ("name", "url", "update_interval_seconds", "enabled"):
            if field in data:
                setattr(zone, field, data[field])
        session.flush()
        return _to_dict(zone)


def delete_local_zone(zone_id: int) -> bool:
    with session_scope() as session:
        zone = session.get(LocalZone, zone_id)
        if zone is None:
            return False
        session.delete(zone)
        session.execute(delete(LocalDnsRecord).where(LocalDnsRecord.zone_id == zone_id))
        return True


def replace_records(zone_id: int, records: Sequence[LocalRecord]) -> None:
    """Atomically swap the stored records for a zone with ``records``."""
    with session_scope() as session:
        session.execute(delete(LocalDnsRecord).where(LocalDnsRecord.zone_id == zone_id))
        if records:
            session.execute(
                insert(LocalDnsRecord),
                [
                    {
                        "zone_id": zone_id,
                        "name": record.name,
                        "rtype": record.rtype,
                        "value": record.value,
                        "ttl": record.ttl,
                    }
                    for record in records
                ],
            )


def _record_to_dict(record: LocalDnsRecord) -> dict[str, Any]:
    return {
        "id": record.id,
        "zone_id": record.zone_id,
        "name": record.name,
        "type": record.rtype,
        "value": record.value,
        "ttl": record.ttl,
    }


def list_records(
    zone_id: int, limit: int | None = None, offset: int = 0
) -> list[dict[str, Any]]:
    stmt = (
        select(LocalDnsRecord)
        .where(LocalDnsRecord.zone_id == zone_id)
        .order_by(LocalDnsRecord.name, LocalDnsRecord.rtype, LocalDnsRecord.value)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        return [_record_to_dict(row) for row in session.scalars(stmt).all()]


def count_records(zone_id: int) -> int:
    stmt = select(LocalDnsRecord.id).where(LocalDnsRecord.zone_id == zone_id)
    with session_scope() as session:
        return len(session.scalars(stmt).all())


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input matches literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _records_filter(search: str | None, rtype: str | None) -> list[ColumnElement[bool]]:
    clauses: list[ColumnElement[bool]] = []
    if search:
        term = f"%{_escape_like(search)}%"
        clauses.append(
            LocalDnsRecord.name.ilike(term, escape="\\")
            | LocalDnsRecord.value.ilike(term, escape="\\")
        )
    if rtype:
        clauses.append(LocalDnsRecord.rtype == rtype.upper())
    return clauses


def list_all_records(
    limit: int | None = None,
    offset: int = 0,
    search: str | None = None,
    rtype: str | None = None,
) -> list[dict[str, Any]]:
    """List records across every zone, annotated with the owning zone's name."""
    stmt = (
        select(LocalDnsRecord, LocalZone.name)
        .join(LocalZone, LocalZone.id == LocalDnsRecord.zone_id)
        .where(*_records_filter(search, rtype))
        .order_by(LocalDnsRecord.name, LocalDnsRecord.rtype, LocalDnsRecord.value)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.execute(stmt).all()
    return [{**_record_to_dict(record), "zone_name": zone_name} for record, zone_name in rows]


def count_all_records(search: str | None = None, rtype: str | None = None) -> int:
    stmt = (
        select(func.count())
        .select_from(LocalDnsRecord)
        .where(*_records_filter(search, rtype))
    )
    with session_scope() as session:
        return session.scalar(stmt) or 0


def record_success(zone_id: int, record_count: int) -> None:
    with session_scope() as session:
        zone = session.get(LocalZone, zone_id)
        if zone is None:
            return
        zone.record_count = record_count
        zone.last_status = f"ok: {record_count} records"
        zone.last_downloaded_at = datetime.now()


def record_failure(zone_id: int, message: str) -> None:
    """Record a failed download. Existing records are left in place."""
    with session_scope() as session:
        zone = session.get(LocalZone, zone_id)
        if zone is None:
            return
        zone.last_status = f"error: {message}"[:255]
        zone.last_downloaded_at = datetime.now()


def load_local_records() -> list[LocalRecord]:
    """Return every record belonging to an enabled zone, for the DNS server."""
    with session_scope() as session:
        enabled_ids = list(
            session.scalars(select(LocalZone.id).where(LocalZone.enabled.is_(True))).all()
        )
        if not enabled_ids:
            return []
        rows = session.scalars(
            select(LocalDnsRecord).where(LocalDnsRecord.zone_id.in_(enabled_ids))
        ).all()
        return [
            LocalRecord(name=row.name, rtype=row.rtype, value=row.value, ttl=row.ttl)
            for row in rows
        ]
