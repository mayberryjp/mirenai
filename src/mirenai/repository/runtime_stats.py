"""Persistence for point-in-time runtime gauges (key/value ``runtime_stats`` table).

The DNS server samples a handful of gauges (cache size, blocklist size, ...) on
the query-flush interval and upserts them here; the API reads the latest
snapshot. ``updated_at`` doubles as the DNS worker's heartbeat.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.repository.models import RuntimeStat


def record_runtime_stats(values: dict[str, int]) -> None:
    if not values:
        return
    with session_scope() as session:
        for key, value in values.items():
            stmt = sqlite_insert(RuntimeStat).values(key=key, value=value)
            stmt = stmt.on_conflict_do_update(
                index_elements=["key"],
                set_={
                    "value": stmt.excluded.value,
                    "updated_at": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)


def load_runtime_stats() -> dict[str, Any]:
    with session_scope() as session:
        rows = session.scalars(select(RuntimeStat)).all()
    updated_at = max((row.updated_at for row in rows), default=None)
    return {
        "stats": {row.key: row.value for row in rows},
        "updated_at": updated_at.isoformat() if updated_at is not None else None,
    }
