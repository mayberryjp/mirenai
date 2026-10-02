"""Persistence for blocklist override (allowlist) domains.

An override domain is removed from every blocklist's parsed domains before they
are written to the blocklist database, so the exemption is applied when lists are
downloaded rather than per DNS query. Overrides are small operator-managed
configuration, so they live in the main configuration database alongside the
blocklist config rows.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mirenai.db import session_scope
from mirenai.repository.models import BlocklistOverride


def _to_dict(override: BlocklistOverride) -> dict[str, Any]:
    return {
        "id": override.id,
        "domain": override.domain,
        "created_at": override.created_at.isoformat(),
        "updated_at": override.updated_at.isoformat(),
    }


def list_overrides(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(BlocklistOverride).order_by(BlocklistOverride.domain)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_overrides() -> int:
    with session_scope() as session:
        return len(session.scalars(select(BlocklistOverride.id)).all())


def create_override(domain: str) -> dict[str, Any]:
    with session_scope() as session:
        override = BlocklistOverride(domain=domain)
        session.add(override)
        session.flush()
        return _to_dict(override)


def delete_override(override_id: int) -> bool:
    with session_scope() as session:
        override = session.get(BlocklistOverride, override_id)
        if override is None:
            return False
        session.delete(override)
    return True


def load_override_domains() -> frozenset[str]:
    """Return every override domain, for filtering blocklists at download time."""
    with session_scope() as session:
        return frozenset(session.scalars(select(BlocklistOverride.domain)).all())
