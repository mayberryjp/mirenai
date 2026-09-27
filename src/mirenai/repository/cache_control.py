"""Persistence for the cross-process cache-flush signal.

The DNS cache lives in the DNS worker process, so the API cannot clear it
directly. Instead a flush request is recorded here (in the ``app_settings``
key/value table) and the worker clears its cache the next time it polls a newer
value. Kept in ``app_settings`` rather than a dedicated one-row table; the key is
not a tunable setting, so it never surfaces in ``GET /settings``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.repository.models import AppSetting

_FLUSH_KEY = "cache_flush_requested_at"


def request_cache_flush() -> str:
    """Record a cache-flush request and return its timestamp (local time, ISO 8601)."""
    requested_at = datetime.now().isoformat(timespec="seconds")
    with session_scope() as session:
        stmt = sqlite_insert(AppSetting).values(key=_FLUSH_KEY, value=requested_at)
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": stmt.excluded.value},
        )
        session.execute(stmt)
    return requested_at


def get_cache_flush_request() -> str | None:
    with session_scope() as session:
        row = session.scalars(select(AppSetting).where(AppSetting.key == _FLUSH_KEY)).first()
        return row.value if row is not None else None
