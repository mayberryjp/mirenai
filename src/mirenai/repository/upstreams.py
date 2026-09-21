"""Persistence for upstream resolvers."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mirenai.db import session_scope
from mirenai.domain.state import UpstreamServer
from mirenai.repository.models import Upstream


def _to_dict(upstream: Upstream) -> dict[str, Any]:
    return {
        "id": upstream.id,
        "name": upstream.name,
        "address": upstream.address,
        "port": upstream.port,
        "protocol": upstream.protocol,
        "enabled": upstream.enabled,
        "priority": upstream.priority,
        "created_at": upstream.created_at.isoformat(),
        "updated_at": upstream.updated_at.isoformat(),
    }


def list_upstreams(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(Upstream).order_by(Upstream.priority, Upstream.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_upstreams() -> int:
    with session_scope() as session:
        return len(session.scalars(select(Upstream.id)).all())


def get_upstream(upstream_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        upstream = session.get(Upstream, upstream_id)
        return _to_dict(upstream) if upstream is not None else None


def create_upstream(data: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        upstream = Upstream(
            name=data.get("name"),
            address=data["address"],
            port=data.get("port", 53),
            protocol=data.get("protocol", "udp"),
            enabled=data.get("enabled", True),
            priority=data.get("priority", 100),
        )
        session.add(upstream)
        session.flush()
        return _to_dict(upstream)


def update_upstream(upstream_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    with session_scope() as session:
        upstream = session.get(Upstream, upstream_id)
        if upstream is None:
            return None
        for field in ("name", "address", "port", "protocol", "enabled", "priority"):
            if field in data:
                setattr(upstream, field, data[field])
        session.flush()
        return _to_dict(upstream)


def delete_upstream(upstream_id: int) -> bool:
    with session_scope() as session:
        upstream = session.get(Upstream, upstream_id)
        if upstream is None:
            return False
        session.delete(upstream)
        return True


def load_upstreams() -> list[UpstreamServer]:
    """Return enabled upstreams (highest priority first) for the DNS server."""
    stmt = (
        select(Upstream)
        .where(Upstream.enabled.is_(True))
        .order_by(Upstream.priority, Upstream.id)
    )
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [
            UpstreamServer(address=row.address, port=row.port, protocol=row.protocol)
            for row in rows
        ]
