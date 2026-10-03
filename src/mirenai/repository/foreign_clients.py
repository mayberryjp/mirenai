"""Persistence for dropped foreign-source IPs.

Aggregated in memory by the DNS server and flushed here on a timer. Each flush
upserts on ``ip``: on conflict the stored ``hits`` is incremented and ``last_seen``
refreshed (``first_seen`` is set once on insert). After writing, the table is
capped to the ``MAX_FOREIGN_CLIENTS`` most recently seen IPs so a flood of spoofed
sources can't grow it without bound.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.foreignclients import ForeignClientAgg
from mirenai.repository.models import ForeignClient

# Cap on retained foreign-source rows (newest by last_seen are kept).
MAX_FOREIGN_CLIENTS = 1000


def _to_dict(row: ForeignClient) -> dict[str, Any]:
    return {
        "id": row.id,
        "ip": row.ip,
        "hits": row.hits,
        "first_seen": row.first_seen.isoformat(),
        "last_seen": row.last_seen.isoformat(),
    }


def record_foreign_clients(rows: list[ForeignClientAgg]) -> None:
    if not rows:
        return
    with session_scope() as session:
        for row in rows:
            stmt = sqlite_insert(ForeignClient).values(ip=row.ip, hits=row.hits)
            stmt = stmt.on_conflict_do_update(
                index_elements=["ip"],
                set_={
                    "hits": ForeignClient.hits + stmt.excluded.hits,
                    "last_seen": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)
        keep = select(ForeignClient.id).order_by(
            ForeignClient.last_seen.desc(), ForeignClient.id.desc()
        ).limit(MAX_FOREIGN_CLIENTS)
        session.execute(delete(ForeignClient).where(ForeignClient.id.not_in(keep)))


def list_foreign_clients(limit: int | None = None, offset: int = 0) -> list[dict[str, Any]]:
    stmt = select(ForeignClient).order_by(ForeignClient.last_seen.desc(), ForeignClient.id.desc())
    if limit is not None:
        stmt = stmt.limit(limit).offset(offset)
    with session_scope() as session:
        rows = session.scalars(stmt).all()
        return [_to_dict(row) for row in rows]


def count_foreign_clients() -> int:
    with session_scope() as session:
        return len(session.scalars(select(ForeignClient.id)).all())
