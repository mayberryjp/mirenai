from typing import Any

from bottle import Bottle, request

from mirenai.api.errors import read_pagination
from mirenai.repository import cache_control as repo
from mirenai.repository import cache_entries as entries_repo
from mirenai.repository import uncacheable as uncacheable_repo


def register_cache_routes(app: Bottle) -> None:
    @app.get("/cache")
    def list_cache() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        search = (request.query.get("search") or "").strip() or None
        updated_at = entries_repo.cache_updated_at()
        if not paginate:
            rows = entries_repo.list_cache_entries(search=search)
            return {"status": "ok", "entries": rows, "total": len(rows), "updated_at": updated_at}
        rows = entries_repo.list_cache_entries(limit=limit, offset=offset, search=search)
        total = entries_repo.count_cache_entries(search=search)
        return {"status": "ok", "entries": rows, "total": total, "updated_at": updated_at}

    @app.get("/cache/uncacheable")
    def list_uncacheable() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        reason = (request.query.get("reason") or "").strip() or None
        if not paginate:
            rows = uncacheable_repo.list_uncacheable(reason=reason)
            return {"status": "ok", "uncacheable": rows, "total": len(rows)}
        rows = uncacheable_repo.list_uncacheable(limit=limit, offset=offset, reason=reason)
        total = uncacheable_repo.count_uncacheable(reason=reason)
        return {"status": "ok", "uncacheable": rows, "total": total}

    @app.post("/cache/flush")
    def flush_cache() -> dict[str, Any]:
        requested_at = repo.request_cache_flush()
        return {"status": "ok", "requested_at": requested_at}
