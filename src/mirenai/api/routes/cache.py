from typing import Any

from bottle import Bottle

from mirenai.repository import cache_control as repo


def register_cache_routes(app: Bottle) -> None:
    @app.post("/cache/flush")
    def flush_cache() -> dict[str, Any]:
        requested_at = repo.request_cache_flush()
        return {"status": "ok", "requested_at": requested_at}
