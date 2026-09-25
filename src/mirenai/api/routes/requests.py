from typing import Any

from bottle import Bottle, request

from mirenai.api.errors import read_pagination
from mirenai.repository import client_requests as repo


def register_request_routes(app: Bottle) -> None:
    @app.get("/requests")
    def list_requests() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        client = request.query.get("client") or None
        if not paginate:
            rows = repo.list_client_requests(client=client)
            return {"status": "ok", "requests": rows, "total": len(rows)}
        rows = repo.list_client_requests(limit=limit, offset=offset, client=client)
        total = repo.count_client_requests(client=client)
        return {"status": "ok", "requests": rows, "total": total}
