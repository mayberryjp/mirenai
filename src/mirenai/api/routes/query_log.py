from typing import Any

from bottle import Bottle

from mirenai.api.errors import read_pagination
from mirenai.repository import query_log as repo


def register_query_routes(app: Bottle) -> None:
    @app.get("/queries")
    def list_queries() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_queries()
            return {"status": "ok", "queries": rows, "total": len(rows)}
        rows = repo.list_queries(limit=limit, offset=offset)
        return {"status": "ok", "queries": rows, "total": repo.count_queries()}

    @app.delete("/queries")
    def reset_queries() -> dict[str, Any]:
        deleted = repo.reset_queries()
        return {"status": "ok", "deleted": deleted}
