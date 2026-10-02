"""Persistence for the trusted-source-network allowlist.

Inbound DNS queries whose source address falls outside every configured subnet
are dropped by the DNS worker (see ``mirenai.domain.network``). An empty table
trusts all clients, so the feature stays opt-in.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mirenai.db import session_scope
from mirenai.repository.models import TrustedNetwork


def _to_dict(row: TrustedNetwork) -> dict[str, Any]:
    return {
        "id": row.id,
        "cidr": row.cidr,
        "description": row.description,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def list_trusted_networks(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(TrustedNetwork).order_by(TrustedNetwork.cidr, TrustedNetwork.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_trusted_networks() -> int:
    with session_scope() as session:
        return len(session.scalars(select(TrustedNetwork.id)).all())


def get_trusted_network(network_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        row = session.get(TrustedNetwork, network_id)
        return _to_dict(row) if row is not None else None


def create_trusted_network(data: dict[str, Any]) -> dict[str, Any]:
    """Insert a trusted subnet. Raises ``IntegrityError`` if the CIDR already exists."""
    with session_scope() as session:
        row = TrustedNetwork(cidr=data["cidr"], description=data.get("description"))
        session.add(row)
        session.flush()
        return _to_dict(row)


def delete_trusted_network(network_id: int) -> bool:
    with session_scope() as session:
        row = session.get(TrustedNetwork, network_id)
        if row is None:
            return False
        session.delete(row)
        return True


def load_trusted_networks() -> list[str]:
    """Return the configured CIDR strings for the DNS server's state snapshot."""
    with session_scope() as session:
        return [row.cidr for row in session.scalars(select(TrustedNetwork)).all()]
