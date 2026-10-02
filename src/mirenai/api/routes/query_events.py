from ipaddress import ip_address
from typing import Any

from bottle import Bottle

from mirenai.api.errors import error, read_int_query
from mirenai.repository import query_events as repo

# Default lookback when the caller omits ``seconds``.
_DEFAULT_SECONDS = 60


def register_query_event_routes(app: Bottle) -> None:
    @app.get("/clients/<ip>/queries")
    def recent_queries(ip: str) -> dict[str, Any]:
        try:
            ip_address(ip)
        except ValueError:
            return error(
                "validation_error", "Invalid request", 422, "client must be a valid IP address"
            )
        seconds, err = read_int_query("seconds")
        if err is not None:
            return err
        if seconds is None:
            seconds = _DEFAULT_SECONDS
        if seconds <= 0:
            return error("validation_error", "Invalid request", 422, "seconds must be positive")
        limit, err = read_int_query("limit")
        if err is not None:
            return err
        if limit is not None and limit <= 0:
            return error("validation_error", "Invalid request", 422, "limit must be positive")
        rows = repo.list_recent_queries(client=ip, seconds=seconds, limit=limit)
        return {
            "status": "ok",
            "client": ip,
            "seconds": seconds,
            "queries": rows,
            "total": len(rows),
        }
