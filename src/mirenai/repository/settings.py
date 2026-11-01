"""Persistence for runtime settings (key/value ``app_settings`` table).

The DNS server and API both read effective settings by merging stored overrides
on top of :class:`RuntimeSettings` defaults, so a fresh database needs no seed
rows. Only known keys are accepted on write.
"""

from __future__ import annotations

from dataclasses import asdict, fields
import json
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from mirenai.db import session_scope
from mirenai.domain.state import RuntimeSettings
from mirenai.domain.notifications import normalize_query_type_flags
from mirenai.integrations.discord import validate_webhook_url
from mirenai.logging import get_logger
from mirenai.repository.models import AppSetting

_BOOL_KEYS = frozenset({
    "cache_enabled", "log_queries", "ipv6_enabled", "drop_private_ptr",
    "discord_notifications_enabled",
})
_INT_KEYS = frozenset(
    {
        "cache_max_ttl",
        "cache_min_ttl",
        "cache_max_entries",
        "refresh_seconds",
        "query_flush_seconds",
    }
)
_FLOAT_KEYS = frozenset({"forward_timeout"})
_STR_KEYS = frozenset({"default_action", "discord_webhook_url"})
_JSON_KEYS = frozenset({"discord_query_types"})

KNOWN_KEYS = _BOOL_KEYS | _INT_KEYS | _FLOAT_KEYS | _STR_KEYS | _JSON_KEYS
log = get_logger("settings")


def _coerce(key: str, raw: str, default: Any) -> Any:
    if key in _JSON_KEYS:
        try:
            decoded = json.loads(raw)
            if not isinstance(decoded, dict) or not all(
                isinstance(name, str) and isinstance(enabled, bool)
                for name, enabled in decoded.items()
            ):
                raise ValueError("discord_query_types must be an object of boolean flags")
            return default | normalize_query_type_flags(decoded)
        except (TypeError, ValueError):
            log.exception("invalid stored Discord query type flags")
            raise
    try:
        if key in _BOOL_KEYS:
            return raw.strip().lower() in {"1", "true", "yes", "on"}
        if key in _INT_KEYS:
            return int(raw)
        if key in _FLOAT_KEYS:
            return float(raw)
        return raw
    except (TypeError, ValueError):
        return default


def _serialize(value: Any) -> str:
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def load_runtime_settings() -> RuntimeSettings:
    defaults = asdict(RuntimeSettings())
    with session_scope() as session:
        stored = {row.key: row.value for row in session.scalars(select(AppSetting)).all()}
    merged = {
        field.name: _coerce(field.name, stored[field.name], defaults[field.name])
        if field.name in stored
        else defaults[field.name]
        for field in fields(RuntimeSettings)
    }
    return RuntimeSettings(**merged)


def get_all_settings() -> dict[str, Any]:
    return asdict(load_runtime_settings())


def update_settings(data: dict[str, Any]) -> dict[str, Any]:
    current = get_all_settings()
    data = dict(data)
    if "discord_query_types" in data:
        data["discord_query_types"] = (
            current["discord_query_types"] | normalize_query_type_flags(data["discord_query_types"])
        )
    effective = current | data
    url = validate_webhook_url(effective["discord_webhook_url"])
    if effective["discord_notifications_enabled"] and not url:
        raise ValueError("discord_webhook_url is required when notifications are enabled")
    with session_scope() as session:
        for key, value in data.items():
            if key not in KNOWN_KEYS:
                continue
            stmt = sqlite_insert(AppSetting).values(key=key, value=_serialize(value))
            stmt = stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={
                    "value": stmt.excluded.value,
                    "updated_at": func.datetime("now", "localtime"),
                },
            )
            session.execute(stmt)
    return get_all_settings()
