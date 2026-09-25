"""Persistence for DNS blocklists.

Blocklist *configuration* (name, URL, cadence, status) lives in the main
configuration database; the downloaded *domains* live in a separate blocklist
database so a large list never bloats the config file. The two are joined by
``Blocklist.id`` / ``BlocklistDomain.blocklist_id``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select

from mirenai.db import blocklist_session_scope, session_scope
from mirenai.repository.models import Blocklist, BlocklistDomain


def _to_dict(blocklist: Blocklist) -> dict[str, Any]:
    return {
        "id": blocklist.id,
        "name": blocklist.name,
        "url": blocklist.url,
        "update_interval_hours": blocklist.update_interval_hours,
        "enabled": blocklist.enabled,
        "domain_count": blocklist.domain_count,
        "last_downloaded_at": blocklist.last_downloaded_at.isoformat()
        if blocklist.last_downloaded_at is not None
        else None,
        "last_status": blocklist.last_status,
        "created_at": blocklist.created_at.isoformat(),
        "updated_at": blocklist.updated_at.isoformat(),
    }


def list_blocklists(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(Blocklist).order_by(Blocklist.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_blocklists() -> int:
    with session_scope() as session:
        return len(session.scalars(select(Blocklist.id)).all())


def get_blocklist(blocklist_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        return _to_dict(blocklist) if blocklist is not None else None


def create_blocklist(data: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        blocklist = Blocklist(
            name=data["name"],
            url=data["url"],
            update_interval_hours=data.get("update_interval_hours", 24),
            enabled=data.get("enabled", True),
        )
        session.add(blocklist)
        session.flush()
        return _to_dict(blocklist)


def update_blocklist(blocklist_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return None
        for field in ("name", "url", "update_interval_hours", "enabled"):
            if field in data:
                setattr(blocklist, field, data[field])
        session.flush()
        return _to_dict(blocklist)


def delete_blocklist(blocklist_id: int) -> bool:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return False
        session.delete(blocklist)
    _delete_domains(blocklist_id)
    return True


def _delete_domains(blocklist_id: int) -> None:
    with blocklist_session_scope() as session:
        session.execute(
            delete(BlocklistDomain).where(BlocklistDomain.blocklist_id == blocklist_id)
        )


def replace_domains(blocklist_id: int, domains: Sequence[str]) -> None:
    """Atomically swap the stored domains for a blocklist with ``domains``."""
    with blocklist_session_scope() as session:
        session.execute(
            delete(BlocklistDomain).where(BlocklistDomain.blocklist_id == blocklist_id)
        )
        if domains:
            session.execute(
                insert(BlocklistDomain),
                [{"blocklist_id": blocklist_id, "domain": domain} for domain in domains],
            )


def list_domains(
    blocklist_id: int, limit: int | None = None, offset: int = 0
) -> list[str]:
    stmt = (
        select(BlocklistDomain.domain)
        .where(BlocklistDomain.blocklist_id == blocklist_id)
        .order_by(BlocklistDomain.domain)
    )
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with blocklist_session_scope() as session:
        return list(session.scalars(stmt).all())


def count_domains(blocklist_id: int) -> int:
    stmt = select(BlocklistDomain.id).where(BlocklistDomain.blocklist_id == blocklist_id)
    with blocklist_session_scope() as session:
        return len(session.scalars(stmt).all())


def record_success(blocklist_id: int, domain_count: int) -> None:
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return
        blocklist.domain_count = domain_count
        blocklist.last_status = f"ok: {domain_count} domains"
        blocklist.last_downloaded_at = datetime.now()


def record_failure(blocklist_id: int, message: str) -> None:
    """Record a failed download. Existing domains are left in place."""
    with session_scope() as session:
        blocklist = session.get(Blocklist, blocklist_id)
        if blocklist is None:
            return
        blocklist.last_status = f"error: {message}"[:255]
        blocklist.last_downloaded_at = datetime.now()


def load_blocklist_domains() -> frozenset[str]:
    """Return every domain belonging to an enabled blocklist, for the DNS server."""
    with session_scope() as session:
        enabled_ids = list(
            session.scalars(select(Blocklist.id).where(Blocklist.enabled.is_(True))).all()
        )
    if not enabled_ids:
        return frozenset()
    with blocklist_session_scope() as session:
        rows = session.scalars(
            select(BlocklistDomain.domain).where(BlocklistDomain.blocklist_id.in_(enabled_ids))
        ).all()
    return frozenset(rows)
