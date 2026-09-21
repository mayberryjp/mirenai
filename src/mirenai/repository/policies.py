"""Persistence for screening policies."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from mirenai.db import session_scope
from mirenai.domain.policy import PolicyRule
from mirenai.repository.models import Policy


def _to_dict(policy: Policy) -> dict[str, Any]:
    return {
        "id": policy.id,
        "client": policy.client,
        "domain": policy.domain,
        "action": policy.action,
        "override_response": policy.override_response,
        "override_ttl": policy.override_ttl,
        "enabled": policy.enabled,
        "description": policy.description,
        "created_at": policy.created_at.isoformat(),
        "updated_at": policy.updated_at.isoformat(),
    }


def list_policies(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(Policy).order_by(Policy.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_policies() -> int:
    with session_scope() as session:
        return len(session.scalars(select(Policy.id)).all())


def get_policy(policy_id: int) -> dict[str, Any] | None:
    with session_scope() as session:
        policy = session.get(Policy, policy_id)
        return _to_dict(policy) if policy is not None else None


def create_policy(data: dict[str, Any]) -> dict[str, Any]:
    with session_scope() as session:
        policy = Policy(
            client=data["client"],
            domain=data["domain"],
            action=data["action"],
            override_response=data.get("override_response"),
            override_ttl=data.get("override_ttl", 300),
            enabled=data.get("enabled", True),
            description=data.get("description"),
        )
        session.add(policy)
        session.flush()
        return _to_dict(policy)


def update_policy(policy_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    with session_scope() as session:
        policy = session.get(Policy, policy_id)
        if policy is None:
            return None
        for field in (
            "client",
            "domain",
            "action",
            "override_response",
            "override_ttl",
            "enabled",
            "description",
        ):
            if field in data:
                setattr(policy, field, data[field])
        session.flush()
        return _to_dict(policy)


def delete_policy(policy_id: int) -> bool:
    with session_scope() as session:
        policy = session.get(Policy, policy_id)
        if policy is None:
            return False
        session.delete(policy)
        return True


def load_rules() -> list[PolicyRule]:
    """Return enabled rules as immutable domain objects for the DNS server."""
    with session_scope() as session:
        rows = session.scalars(select(Policy).where(Policy.enabled.is_(True))).all()
        return [
            PolicyRule(
                client=row.client,
                domain=row.domain,
                action=row.action,
                override_response=row.override_response,
                override_ttl=row.override_ttl,
            )
            for row in rows
        ]
