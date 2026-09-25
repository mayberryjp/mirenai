from typing import Any

from bottle import Bottle

from mirenai.api.errors import error, parse_body, read_pagination
from mirenai.api.schemas import HostUpdate
from mirenai.repository import hosts as repo


def register_host_routes(app: Bottle) -> None:
    @app.get("/hosts")
    def list_hosts() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_hosts()
            return {"status": "ok", "hosts": rows, "total": len(rows)}
        rows = repo.list_hosts(limit=limit, offset=offset)
        return {"status": "ok", "hosts": rows, "total": repo.count_hosts()}

    @app.get("/hosts/<host_id:int>")
    def get_host(host_id: int) -> dict[str, Any]:
        row = repo.get_host(host_id)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "host": row}

    @app.put("/hosts/<host_id:int>")
    def update_host(host_id: int) -> dict[str, Any]:
        model, err = parse_body(HostUpdate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        row = repo.update_host(host_id, model.model_dump(exclude_unset=True))
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "host": row}

    @app.delete("/hosts/<host_id:int>")
    def delete_host(host_id: int) -> dict[str, Any]:
        if not repo.delete_host(host_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": host_id}
