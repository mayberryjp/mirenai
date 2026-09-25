"""Persistence for the local hosts seen by the DNS server.

Every query's source IP is recorded in a dedicated ``localhosts.db`` database.
Per-IP counts are aggregated in memory by the DNS server and flushed here in
batches: each flush upserts on ``ip``, incrementing ``query_count`` and
refreshing ``last_seen`` (``first_seen`` is set once on insert).
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import hosts_session_scope
from mirenai.repository.models import Host


def _to_dict(host: Host) -> dict[str, Any]:
    return {
        "id": host.id,
        "ip": host.ip,
        "device_name": host.device_name,
        "query_count": host.query_count,
        "first_seen": host.first_seen.isoformat(),
        "last_seen": host.last_seen.isoformat(),
    }


def load_known_hosts() -> set[str]:
    with hosts_session_scope() as session:
        return set(session.scalars(select(Host.ip)).all())


def record_hosts(counts: dict[str, int]) -> None:
    if not counts:
        return
    with hosts_session_scope() as session:
        for ip, count in counts.items():
            stmt = sqlite_insert(Host).values(ip=ip, query_count=count)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ip"],
                set_={
                    "query_count": Host.query_count + stmt.excluded.query_count,
                    "last_seen": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)


def list_hosts(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(Host).order_by(Host.last_seen.desc(), Host.id)
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with hosts_session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_hosts() -> int:
    with hosts_session_scope() as session:
        return len(session.scalars(select(Host.id)).all())


def get_host(host_id: int) -> dict[str, Any] | None:
    with hosts_session_scope() as session:
        host = session.get(Host, host_id)
        return _to_dict(host) if host is not None else None


def update_host(host_id: int, data: dict[str, Any]) -> dict[str, Any] | None:
    with hosts_session_scope() as session:
        host = session.get(Host, host_id)
        if host is None:
            return None
        if "device_name" in data:
            host.device_name = data["device_name"]
        session.flush()
        return _to_dict(host)


def delete_host(host_id: int) -> bool:
    with hosts_session_scope() as session:
        host = session.get(Host, host_id)
        if host is None:
            return False
        session.delete(host)
        return True
